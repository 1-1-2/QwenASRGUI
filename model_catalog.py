"""Model asset catalog: one source of truth for downloads, checks and manifests.

Pure standard library. No huggingface_hub / modelscope / torch / requests.
All comments and docstrings are intentionally in English.

`models.json` is machine data, not documentation: every `size_bytes` and
`sha256` that is not verified against the real remote file stays null. Callers
must treat null as "unknown" instead of substituting a guess.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import app_paths

_CATALOG_NAME = "models.json"
_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"
_HTML_MAGICS = (b"<!doctype html", b"<html", b"<!DOCTYPE HTML")
_UA = "Mozilla/5.0 (compatible; QwenASR-downloader)"
_CACHE: dict | None = None
_CACHE_PATH: Path | None = None


class CatalogError(Exception):
    """Raised when the catalog is malformed or an operation is refused."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def catalog_path() -> Path:
    """Locate models.json.

    The catalog is a shipped read-only resource, so it resolves through
    ``app_paths``: source tree in development, ``_internal/`` in a onedir
    build, the _MEIPASS temp dir in onefile. A copy beside the launcher wins
    over the bundled one - that is how a user repairs or overrides the
    catalog without rebuilding (and how pre-G2 packages were laid out).
    """
    beside = app_paths.app_dir() / _CATALOG_NAME
    if beside.is_file():
        return beside
    return app_paths.resource_path(_CATALOG_NAME)


def load_catalog(path: Path | str | None = None, refresh: bool = False) -> dict:
    """Load and validate the catalog. Results are cached by resolved path."""
    global _CACHE, _CACHE_PATH
    p = Path(path) if path else catalog_path()
    p = p.resolve()
    if not refresh and _CACHE is not None and _CACHE_PATH == p:
        return _CACHE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"models.json not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"models.json is not valid JSON: {exc}") from exc
    _validate(doc)
    _CACHE, _CACHE_PATH = doc, p
    return doc


def _validate(doc: dict) -> None:
    if doc.get("schema") != 1:
        raise CatalogError("unsupported models.json schema")
    if not isinstance(doc.get("sources"), dict) or not doc["sources"]:
        raise CatalogError("missing sources")
    if not isinstance(doc.get("bundles"), list) or not doc["bundles"]:
        raise CatalogError("missing bundles")
    ids = set()
    for b in doc["bundles"]:
        for key in ("id", "label", "target_dir", "files"):
            if key not in b:
                raise CatalogError(f"bundle missing {key!r}: {b!r}")
        if b["id"] in ids:
            raise CatalogError(f"duplicate bundle id: {b['id']}")
        ids.add(b["id"])
        if not isinstance(b["files"], list) or not b["files"]:
            raise CatalogError(f"bundle {b['id']} has no files")
        for f in b["files"]:
            if "name" not in f or "source" not in f:
                raise CatalogError(f"bad file entry in {b['id']}: {f!r}")
            if f["source"] not in doc["sources"]:
                raise CatalogError(f"unknown source {f['source']!r} in {b['id']}")
            if f.get("fallback_source") and f["fallback_source"] not in doc["sources"]:
                raise CatalogError(f"unknown fallback source in {b['id']}")
            sha = f.get("sha256")
            if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
                raise CatalogError(f"bad sha256 for {f['name']}")
            size = f.get("size_bytes")
            if size is not None and (not isinstance(size, int) or size < 0):
                raise CatalogError(f"bad size_bytes for {f['name']}")


def get_bundle(bundle_id: str, doc: dict | None = None) -> dict:
    for b in (doc or load_catalog())["bundles"]:
        if b["id"] == bundle_id:
            return b
    raise CatalogError(f"unknown bundle: {bundle_id}")


def source_base(name: str, doc: dict | None = None) -> str:
    src = (doc or load_catalog())["sources"].get(name)
    if not src:
        raise CatalogError(f"unknown source: {name}")
    return src["base"].rstrip("/")


# ---------------------------------------------------------------------------
# URL resolution and mirrors
# ---------------------------------------------------------------------------

def apply_mirror(url: str, mirror_base: str, official: str | None = None) -> str:
    """Rewrite only huggingface.co URLs. Other hosts are left untouched."""
    official = official or load_catalog()["mirrors"]["official"]
    if mirror_base and url.startswith(official):
        return mirror_base.rstrip("/") + url[len(official):]
    return url


def file_urls(file: dict, doc: dict | None = None, mirror_base: str = "") -> list[str]:
    """Return ordered candidate URLs (primary first, fallback second)."""
    doc = doc or load_catalog()
    if file.get("url_override"):
        urls = [file["url_override"]]
    else:
        urls = [source_base(file["source"], doc) + "/" + file["name"]]
    if file.get("fallback_source"):
        urls.append(source_base(file["fallback_source"], doc) + "/" + file["name"])
    if mirror_base:
        urls = [apply_mirror(u, mirror_base, doc["mirrors"]["official"]) for u in urls]
        # Keep the untouched official URLs as a last resort so a broken mirror
        # never makes the asset unreachable.
        for u in list(urls):
            if u not in urls:
                urls.append(u)
    return urls


# ---------------------------------------------------------------------------
# Size / integrity helpers
# ---------------------------------------------------------------------------

def human_size(n: int | None) -> str:
    """Format bytes as a human-readable string; None stays 'unknown'."""
    if n is None:
        return "unknown"
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KiB", 1 << 10), ("MiB", 1 << 20), ("GiB", 1 << 30)):
        if n < div * 1024 or unit == "GiB":
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def file_problem(path: Path) -> str | None:
    """Return a reason string when a downloaded file is not a real asset."""
    if not path.exists():
        return "missing"
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        return f"unreadable: {exc}"
    if head.startswith(_LFS_MAGIC):
        return "Git LFS pointer (install git-lfs or download the real file)"
    low = head.lstrip().lower()
    if any(low.startswith(m) for m in _HTML_MAGICS):
        return "HTML page returned instead of file (login / mirror error)"
    return None


def probe_remote_size(url: str, timeout: float = 10.0) -> int | None:
    """Best-effort Content-Length / Content-Range probe. Never raises."""
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Range": "bytes=0-0"}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            cr = resp.headers.get("Content-Range")
            if cr and "/" in cr:
                tail = cr.rsplit("/", 1)[1].strip()
                if tail.isdigit():
                    return int(tail)
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception:
        return None
    return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def free_disk_bytes(dest: Path) -> int:
    """Free bytes on the filesystem that will hold dest (creates parents)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    probe = dest.parent if dest.parent.exists() else Path.cwd()
    return shutil.disk_usage(str(probe)).free


def check_disk(dest: Path, needed: int | None, safety: int = 64 << 20) -> str | None:
    """Return a warning string when free space is provably insufficient."""
    if not needed:
        return None
    free = free_disk_bytes(dest)
    if free < needed + safety:
        return (f"insufficient disk space: need {human_size(needed + safety)}, "
                f"free {human_size(free)}")
    return None


# ---------------------------------------------------------------------------
# Error classification (used by the UI to give an actionable next step)
# ---------------------------------------------------------------------------

def classify_error(exc: BaseException) -> dict:
    """Map a download exception to a stable code plus a user-facing hint."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code in (401, 403):
            return {"code": "http_forbidden", "hint": "source refused access; try a mirror or the manual download list"}
        if code == 404:
            return {"code": "http_not_found", "hint": "file not found on this source; switch source or re-check the catalog"}
        if code == 416:
            return {"code": "range_complete", "hint": "local file already complete"}
        if 500 <= code < 600:
            return {"code": "http_server", "hint": "upstream server error; retry later or use another mirror"}
        return {"code": "http_error", "hint": f"HTTP {code}; try a mirror"}
    if isinstance(exc, urllib.error.URLError):
        reason = str(getattr(exc, "reason", exc)).lower()
        if "timed out" in reason or "timeout" in reason:
            return {"code": "timeout", "hint": "connection timed out; switch to a closer mirror or use the offline download list"}
        if "ssl" in reason or "certificate" in reason:
            return {"code": "tls", "hint": "TLS verification failed; check system time / CA bundle, or use manual download"}
        if "name or service" in reason or "getaddrinfo" in reason or "nodename" in reason:
            return {"code": "dns", "hint": "DNS resolution failed; check network or set a mirror endpoint"}
        return {"code": "network", "hint": "network unreachable; check proxy / firewall"}
    if isinstance(exc, ssl.SSLError):
        return {"code": "tls", "hint": "TLS handshake failed; check system time / CA bundle"}
    if isinstance(exc, TimeoutError):
        return {"code": "timeout", "hint": "connection timed out; switch mirror"}
    if isinstance(exc, OSError):
        return {"code": "disk", "hint": "local I/O failed; check disk space and write permission"}
    return {"code": "unknown", "hint": "download failed; see the run log for details"}


# ---------------------------------------------------------------------------
# Status / verification
# ---------------------------------------------------------------------------

def bundle_dir(model_root: Path, bundle: dict,
               model_dir: Path | None = None) -> Path:
    """Resolve a bundle's ``target_dir`` to an absolute directory.

    ``target_dir`` in models.json is written **relative to the application
    root**, not relative to ``ov_models``: e.g. ``ov_models/qwen3_asr_int8``,
    ``ov_models/diarization``, ``ffmpeg``, ``crispasr``.

    Wave 12: the WebView backend used to pass ``model_dir`` (which already
    *is* ``<app>/ov_models``) as ``model_root``. That produced
    ``<app>/ov_models/ov_models/qwen3_asr_int8`` — one extra ``ov_models``
    layer — so the download centre always reported every bundle as missing.

    ``model_dir`` is an optional override for users who pointed
    ``settings.model_dir`` somewhere else. When given, the leading
    ``ov_models`` segment of ``target_dir`` is **replaced** by it (keeping the
    downloader's "model_dir is the ov_models folder" convention), while
    non-model bundles such as ``ffmpeg`` / ``crispasr`` stay relative to
    ``model_root``. Omitting it keeps the historical behaviour intact.
    """
    rel = Path(bundle["target_dir"])
    parts = rel.parts
    if model_dir is not None and parts and parts[0] == "ov_models":
        return Path(model_dir).joinpath(*parts[1:])
    return Path(model_root) / rel


def bundle_status(model_root: Path, bundle: dict, doc: dict | None = None,
                  model_dir: Path | None = None) -> dict:
    """Inspect local files. `size_bytes` comes from disk, not from guesses."""
    root = bundle_dir(model_root, bundle, model_dir)
    files = []
    for f in bundle["files"]:
        p = root / f["name"]
        problem = file_problem(p)
        actual = p.stat().st_size if p.exists() and not problem else 0
        expected_sha = f.get("sha256")
        sha_ok = None
        if expected_sha and actual and not problem:
            sha_ok = sha256_file(p) == expected_sha
        files.append({
            "name": f["name"],
            "path": str(p),
            "present": problem is None,
            "problem": problem,
            "size_bytes": actual or None,
            "declared_size_bytes": f.get("size_bytes"),
            "size_hint": f.get("size_hint"),
            "sha256_expected": expected_sha,
            "sha256_ok": sha_ok,
        })
    present = sum(1 for x in files if x["present"])
    return {
        "id": bundle["id"],
        "label": bundle["label"],
        "target_dir": str(root),
        "modes": bundle.get("modes", []),
        "optional": bool(bundle.get("optional")),
        "size_hint": bundle.get("size_hint"),
        "size_hint_source": bundle.get("size_hint_source"),
        "provider": bundle.get("provider"),
        "files": files,
        "present_count": present,
        "total_count": len(files),
        "complete": present == len(files) and all(x["sha256_ok"] is not False for x in files),
        "bytes_on_disk": sum(x["size_bytes"] or 0 for x in files),
        "known_bytes_verified": all(x["sha256_ok"] is not False for x in files),
    }


def catalog_status(model_root: Path, doc: dict | None = None,
                   model_dir: Path | None = None) -> dict:
    doc = doc or load_catalog()
    items = [bundle_status(model_root, b, doc, model_dir)
             for b in doc["bundles"]]
    return {
        "schema": doc["schema"],
        "model_root": str(model_root),
        "model_dir": str(model_dir) if model_dir is not None else "",
        "mirrors": doc["mirrors"],
        "bundles": items,
        "complete": [x["id"] for x in items if x["complete"]],
        "incomplete": [x["id"] for x in items if not x["complete"]],
        "total_bytes_on_disk": sum(x["bytes_on_disk"] for x in items),
        "note": doc["note"],
    }


def verify_bundle(model_root: Path, bundle: dict,
                  model_dir: Path | None = None) -> tuple[bool, str]:
    st = bundle_status(model_root, bundle, None, model_dir)
    for x in st["files"]:
        if x["problem"]:
            return False, f"{x['name']}: {x['problem']}"
        if x["sha256_ok"] is False:
            return False, f"{x['name']}: SHA256 mismatch"
    return True, "OK"


def import_local_bundle(src_dir: Path, model_root: Path, bundle: dict,
                        model_dir: Path | None = None) -> dict:
    """Copy (or verify in place) files from a manually downloaded directory.

    Only files listed in the catalog are touched; unknown files are ignored.
    """
    src = Path(src_dir)
    if not src.is_dir():
        raise CatalogError(f"not a directory: {src}")
    dst = bundle_dir(model_root, bundle, model_dir)
    if src.resolve() == dst.resolve():
        copied = 0
    else:
        dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in bundle["files"]:
            s = src / f["name"]
            if not s.is_file():
                continue
            d = dst / f["name"]
            if d.exists() and d.stat().st_size == s.stat().st_size:
                continue
            shutil.copy2(s, d)
            copied += 1
    ok, msg = verify_bundle(model_root, bundle, model_dir)
    return {"ok": ok, "message": msg, "copied": copied,
            "status": bundle_status(model_root, bundle, None, model_dir)}


# ---------------------------------------------------------------------------
# L2: human-readable manifest + ready-to-run downloader commands
# ---------------------------------------------------------------------------

def _manifest_path(path: Path) -> str:
    """Render a path in a manifest with POSIX separators on every OS.

    The download manifest is a human-readable text artefact that users copy
    into aria2c / download managers. Emitting os-native separators made it
    differ between Linux and Windows (``ov_models/qwen3_asr_int8`` vs
    ``ov_models\\qwen3_asr_int8``), which broke the Wave 2 regression on the
    Windows runner. Windows file APIs and aria2c both accept forward slashes,
    so ``as_posix()`` is safe here and makes the output deterministic.
    """
    return Path(path).as_posix()


def build_manifest_text(model_root: Path, bundle_ids: list[str],
                        mirror_base: str = "", doc: dict | None = None,
                        model_dir: Path | None = None) -> str:
    """Produce a .txt list with URLs, target paths, hashes and aria2c commands.

    ``model_root`` is the application root (``ffmpeg`` / ``crispasr`` bundles
    live there); ``model_dir`` optionally overrides the ``ov_models`` folder
    for ``ov_models/*`` bundles. Both must agree with :func:`catalog_status`
    or the manifest would tell users to download into a different place than
    the download centre checks.
    """
    doc = doc or load_catalog()
    chosen = [get_bundle(b, doc) for b in bundle_ids] if bundle_ids else doc["bundles"]
    lines: list[str] = []
    lines.append("QwenASRGUI · model download manifest")
    lines.append("=" * 60)
    lines.append(f"Target root : {_manifest_path(Path(model_root).resolve())}")
    lines.append(f"Mirror      : {mirror_base or '(official, no mirror)'}")
    lines.append("")
    lines.append("IMPORTANT: size_bytes / sha256 are shown as UNKNOWN unless verified")
    lines.append("against the real remote file. No estimated size is used for progress.")
    lines.append("")
    all_cmds: list[str] = []
    for b in chosen:
        lines.append(f"[{b['id']}] {b['label']}")
        lines.append(f"  target : {_manifest_path(bundle_dir(model_root, b, model_dir))}")
        if b.get("size_hint"):
            lines.append(f"  approx : {b['size_hint']} (from {b.get('size_hint_source', 'source')}; estimate only)")
        lines.append("")
        for f in b["files"]:
            urls = file_urls(f, doc, mirror_base)
            sha = f.get("sha256") or "UNKNOWN"
            hint = f.get("size_hint")
            lines.append(f"  - {f['name']}")
            lines.append(f"      primary : {urls[0]}")
            for u in urls[1:]:
                lines.append(f"      alt     : {u}")
            lines.append(f"      sha256  : {sha}")
            if hint:
                lines.append(f"      approx  : {hint} (estimate, not for progress)")
            dest = bundle_dir(model_root, b, model_dir) / f["name"]
            all_cmds.append(f'aria2c -x 8 -s 8 -k 1M -c -d "{_manifest_path(dest.parent)}" -o "{f["name"]}" "{urls[0]}"')
        lines.append("")
    lines.append("=" * 60)
    lines.append("aria2c commands (copy / paste, resumable, multi-connection)")
    lines.append("Install aria2c first; on Windows you can also paste the URLs")
    lines.append("into a download manager. After downloading, use the Import")
    lines.append("Local Model button to validate and place the files.")
    lines.append("")
    lines.extend(all_cmds)
    lines.append("")
    lines.append("Hugging Face CLI alternative (optional external tool, not bundled):")
    for b in chosen:
        if b.get("target_dir"):
            lines.append(f'# hf download <repo> --local-dir "{_manifest_path(bundle_dir(model_root, b, model_dir))}"')
    lines.append("")
    return "\n".join(lines)
