"""Install pinned official monitoring binaries into ignored artifacts (Windows x64)."""
import hashlib
import json
import platform
import urllib.request
import zipfile
from pathlib import Path


VERSIONS = {"prometheus": "3.5.0", "alertmanager": "0.28.1"}


def main():
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("This local installer targets Windows x64 only")
    root = Path("artifacts/local-pilot-tools")
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, version in VERSIONS.items():
        base = f"https://github.com/prometheus/{name}/releases/download/v{version}/"
        filename = f"{name}-{version}.windows-amd64.zip"
        with urllib.request.urlopen(base + "sha256sums.txt", timeout=30) as response:
            checksums = response.read(1_048_576).decode()
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1].lstrip("*") == filename)
        archive = root / filename
        if not archive.exists():
            partial = archive.with_suffix(".partial")
            try:
                with urllib.request.urlopen(base + filename, timeout=60) as response, partial.open("wb") as target:
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 256 * 1024 * 1024:
                            raise ValueError("Archive exceeds size limit")
                        target.write(chunk)
                partial.replace(archive)
            finally:
                partial.unlink(missing_ok=True)
        with archive.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("Official archive checksum mismatch")
        executables = [name + ".exe", "promtool.exe" if name == "prometheus" else "amtool.exe"]
        with zipfile.ZipFile(archive) as source:
            for executable in executables:
                # Extract only explicit members to fixed paths, never archive paths.
                data = source.read(f"{name}-{version}.windows-amd64/{executable}")
                (root / executable).write_bytes(data)
        manifest.append({"name": name, "version": version, "archive": filename, "url": base + filename, "sha256": actual})
        print(json.dumps({"installed": name, "version": version, "sha256": actual}), flush=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
