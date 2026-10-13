"""Optional second image source: DICOM studies in Azure Blob Storage, laid out as
<prefix><patient>/<study>/<series>/<files>.

The coordinator picks studies in the coordinator page. Each picked study becomes a case; its files are
downloaded to a temporary folder only while its volume is prepared, then deleted again.

Settings: AZURE_STORAGE_CONNECTION_STRING (account key or SAS), AZURE_STORAGE_CONTAINER, AZURE_STORAGE_PREFIX.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config  # noqa: F401  (loads .env before settings are read)

# Files stored next to the DICOMs that are never images to import
SKIP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".json", ".html", ".htm", ".i16", ".txt", ".xml", ".pdf", ".csv"}

_client = None


def enabled():
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip())


def prefix():
    p = os.environ.get("AZURE_STORAGE_PREFIX", "").strip().strip("/")
    return p + "/" if p else ""


def where():
    return f'{os.environ.get("AZURE_STORAGE_CONTAINER", "")}/{prefix()}'


def container():
    global _client
    if _client is None:
        from azure.storage.blob import ContainerClient
        _client = ContainerClient.from_connection_string(
            os.environ["AZURE_STORAGE_CONNECTION_STRING"].strip(), os.environ.get("AZURE_STORAGE_CONTAINER", "").strip())
    return _client


def _subfolders(pfx):
    return sorted(b.name[len(pfx):].rstrip("/") for b in container().walk_blobs(name_starts_with=pfx, delimiter="/")
                  if b.name.endswith("/"))


def list_studies():
    """Every <patient>/<study> folder under the prefix."""
    pfx = prefix()
    patients = _subfolders(pfx)
    with ThreadPoolExecutor(8) as ex:
        studies = list(ex.map(lambda p: _subfolders(f"{pfx}{p}/"), patients))
    return [{"path": f"{p}/{s}", "patient": p, "study": s} for p, ss in zip(patients, studies) for s in ss]


def study_files(study):
    base = f"{prefix()}{study.strip('/')}/"
    return [b for b in container().list_blobs(name_starts_with=base)
            if os.path.splitext(b.name)[1].lower() not in SKIP_EXT and not b.name.endswith("/")]


def signature(files):
    return "blob:{}:{}:{}".format(len(files), sum(b.size for b in files),
                                  int(max(b.last_modified.timestamp() for b in files)))


def download(study, files, dest):
    """Download a study's files into dest, keeping the series subfolders. Returns the total bytes."""
    base = f"{prefix()}{study.strip('/')}/"
    dest = Path(dest).resolve()
    cc = container()

    def get(b):
        p = (dest / b.name[len(base):]).resolve()
        if dest not in p.parents:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            cc.download_blob(b.name, max_concurrency=2).readinto(f)

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(get, files))
    return sum(b.size for b in files)
