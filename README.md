# IDRF Reader

A small web application for **blinded scoring of neuroblastoma image-defined risk factors (IDRFs)** on contrast-enhanced CT, at diagnosis and after induction chemotherapy.

Radiologists open a link in their browser, sign in, and score the 20 IDRFs defined by the International Neuroblastoma Risk Group. The study coordinator manages cases and readers, follows progress, and exports the results. Images stay on the machine running the app; browsers receive pixel data and voxel spacing only, never DICOM header fields.

![Reader view](docs/reader.png)

## Features

**For radiologists**
- Axial, coronal and sagittal views with scroll, window/level presets, zoom, pan and a linked crosshair
- The 20 IDRFs as a checklist (Absent / Present / N/A), with one click to mark a whole region as not imaged
- Blinded: each scan shows only a random code, cases come in a different random order per reader, and nothing reveals the timepoint or which scans belong to the same patient
- Drafts save automatically; time per case is recorded automatically; the next case preloads in the background

**For the study coordinator**
- Automatic import of DICOM folders, choosing the contrast-enhanced axial series (JPEG, JPEG-LS and JPEG 2000 compressed files supported through GDCM)
- Reader accounts with two roles: primary (all cases) and second (double-read cases only)
- Progress per reader, median time per case and estimated time left
- Cohen's kappa and per-item agreement between readers
- Before-and-after table per patient: IDRF count at diagnosis and after induction, cleared or not, which persisted
- CSV export of every saved read

![Coordinator view](docs/coordinator.png)

## Requirements

- Python 3.10 to 3.12
- A recent Chrome, Edge or Firefox for the readers
- Disk space of roughly twice the size of the DICOM data

## Quick start

### Windows

1. Install Python from [python.org](https://www.python.org/downloads/) and tick **Add Python to PATH** during installation.
2. Double-click **`run.bat`**. The first time, it creates a `.env` settings file and stops.
3. Open `.env` in Notepad, change `ADMIN_PASSWORD`, save.
4. Double-click **`run.bat`** again. It sets up a Python environment (first time only) and starts the app.
5. Open **http://localhost:8080** and sign in as `admin` with your password.

### Linux or macOS

```bash
./run.sh                 # first run creates .env and stops
nano .env                # change ADMIN_PASSWORD
./run.sh                 # sets up .venv (first time) and starts the app
```

Then open **http://localhost:8080**.

### Manual setup (any system)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                             # then edit ADMIN_PASSWORD
.venv/bin/python run.py                          # Windows: .venv\Scripts\python run.py
```

## Try it with synthetic test images

The project includes a generator of synthetic CT phantoms (drawn by code, no patient data) so the full workflow can be checked before real images arrive:

```bash
.venv/bin/python tools/make_demo_data.py          # Windows: .venv\Scripts\python tools\make_demo_data.py
```

Then, in the coordinator page, choose **Scan for new images**. Eight test cases appear: four patients, each before and after induction. Add a test reader, open the app in a private browser window, sign in as that reader and score a case.

**Start clean before real data:** stop the app and delete the `data` folder.

## Adding study images

1. **Remove patient identity first.** The app never sends DICOM header fields to browsers, but the files themselves sit on the machine.
2. Put **one folder per scan** inside `data/dicom/`. Subfolders are fine. Use neutral names, never patient names or record numbers:

   ```
   data/dicom/
     NB001_dx/      all DICOM files of the diagnostic CT
     NB001_post/    all DICOM files of the post-induction CT
     NB002_dx/
     ...
   ```

3. The app picks the **contrast-enhanced axial CT series** in each folder, preferring slices of about 2.5 mm. The coordinator page shows which series was used and any warnings.
4. Optionally add `data/cases.csv` so patients and timepoints fill in automatically:

   ```csv
   folder,patient,timepoint,double_read,series
   NB001_dx,NB001,diagnosis,yes,
   NB001_post,NB001,post_induction,yes,
   NB002_dx,NB002,diagnosis,,portal
   ```

   `timepoint` is `diagnosis` or `post_induction`. `double_read` is `yes` for cases the second reader should also score. `series` optionally forces a series by part of its description. Empty cells are ignored, so values set in the coordinator page are kept.

5. In the coordinator page, choose **Scan for new images**. Each folder becomes a case with a random code such as `NB-7KQ4`. Unchanged folders are skipped, so scanning again is always safe.

## Readers

In the coordinator page, under **Readers**, add each radiologist with a name, username, password and role:

- **Primary reader** scores every case.
- **Second reader** scores only the cases marked double read; this produces the inter-reader agreement figures.

Readers sign in, and the next unscored case opens by itself. They mark each IDRF, then choose **Save and next case**.

Radiologists must never use the `admin` account: it shows which scans belong to the same patient, which would break the blinding.

## Running on a hospital server

In `.env`:

```ini
HOST=0.0.0.0          # accept connections from other computers on the network
PORT=8080
```

Readers then open `http://<server-address>:8080`. Open that port in the server firewall for the hospital network only.

- **Never expose the app to the internet.** Remote access should go through the hospital VPN.
- **HTTPS:** put the app behind the hospital's reverse proxy, then set `COOKIE_SECURE=1`.
- **Run as a service** so it restarts with the server. A minimal systemd unit for Linux:

  ```ini
  # /etc/systemd/system/idrf-reader.service
  [Unit]
  Description=IDRF Reader
  After=network.target

  [Service]
  WorkingDirectory=/opt/idrf-reader
  ExecStart=/opt/idrf-reader/.venv/bin/python run.py
  Restart=always
  User=idrf

  [Install]
  WantedBy=multi-user.target
  ```

  ```bash
  sudo systemctl enable --now idrf-reader
  ```

## Running on Azure App Service (Linux)

Use a Linux Python 3.12 web app on the Basic (B1) plan or higher, with a single instance: the database is SQLite, so do not scale out.

1. **Configuration > General settings**: Startup command `python run.py`. Turn on **Always on** and **HTTPS only**.
2. **Environment variables**: add

   | Name | Value |
   |---|---|
   | `ADMIN_PASSWORD` | a strong password |
   | `HOST` | `0.0.0.0` |
   | `PORT` | `8000` |
   | `WEBSITES_PORT` | `8000` |
   | `DATA_DIR` | `/home/data` (the persistent storage; the app folder itself is replaced on every deploy) |
   | `SQLITE_JOURNAL_MODE` | `DELETE` |
   | `COOKIE_SECURE` | `1` |
   | `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |
   | `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER`, `AZURE_STORAGE_PREFIX` | optional, see [Images from Azure Blob Storage](#images-from-azure-blob-storage) |

3. Deploy from GitHub through **Deployment Center**.
4. Add studies from Azure Blob Storage (below), or upload de-identified DICOM folders to `/home/data/dicom/`, for example as a zip through the Kudu API, which extracts it in place:

   ```bash
   curl -X PUT -u '<deployment-user>' --data-binary @scans.zip https://<app>.scm.azurewebsites.net/api/zip/data/dicom/
   ```

   Then choose **Scan for new images** in the coordinator page.

An App Service app is reachable from the internet by default. Limit it under **Networking > Access restrictions** (for example to the hospital's public IP addresses) or put sign-in in front of it with **Authentication**, and confirm with your institution that the de-identified images may be stored in the cloud.

## Images from Azure Blob Storage

Studies can come straight from a blob container laid out as `<prefix>/<patient>/<study>/<series>/<DICOM files>`. Set `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER` and `AZURE_STORAGE_PREFIX`; a **Cloud storage** list then appears in the coordinator page.

- Tick the studies to be read and choose **Add selected**. Each becomes a blinded case, with the patient filled in from the patient folder. Set the timepoint in the case list.
- Each study is downloaded to a temporary folder only while it is prepared; only the prepared volume is kept in `DATA_DIR/cache`. Scanning again re-prepares a study only if its files changed.
- A cloud case no reader has started can be removed with **Remove**, and added again later.
- A read-only SAS (permissions: read and list on the container) is safer than the account key.

## Settings

All settings live in `.env` (copy from `.env.example`). Real environment variables take precedence.

| Setting | Default | What it does |
|---|---|---|
| `ADMIN_PASSWORD` | must be changed | Password of the `admin` coordinator account |
| `HOST` | `127.0.0.1` | `127.0.0.1` for this computer only; `0.0.0.0` to serve the network |
| `PORT` | `8080` | Port the app listens on |
| `DATA_DIR` | `data` | Folder for images, database and prepared cache |
| `SESSION_HOURS` | `12` | How long a sign-in lasts |
| `MAX_INPLANE` | `512` | Largest in-plane matrix sent to browsers; `256` loads four times lighter |
| `PREFERRED_THICKNESS` | `2.5` | Slice thickness in mm preferred when a study has several contrast series |
| `COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS |
| `AZURE_STORAGE_CONNECTION_STRING` | empty | Connection string of the blob storage holding DICOM studies; empty turns the cloud list off |
| `AZURE_STORAGE_CONTAINER` | empty | Blob container with the studies |
| `AZURE_STORAGE_PREFIX` | empty | Folder inside the container where the patient folders start |
| `SQLITE_JOURNAL_MODE` | `WAL` | Set to `DELETE` when `DATA_DIR` is on a network share, such as Azure App Service |

## Backups

Scores are stored in `data/idrf.db`. To take a safe copy while the app is running:

```bash
.venv/bin/python tools/backup.py
```

Copies go to `data/backups/`; the 30 most recent are kept. Schedule it nightly with cron or Windows Task Scheduler.

## Results export

The CSV export has one row per saved read: reader, role, case code, folder, patient, timepoint, seconds, confidence, number of IDRFs present, the answer for each of the 20 items, notes and save time.

## Privacy and security

- `data/` and `.env` are excluded by `.gitignore`. **Never commit patient images, the database or passwords.** Keeping this repository private is recommended.
- Passwords are hashed with PBKDF2-SHA256. Sessions expire after `SESSION_HOURS`.
- Sign-ins, failed sign-ins, saves, reader changes and exports are logged in the `audit` table.
- Browsers receive only pixel data and voxel spacing; DICOM header fields stay on the server.

## Troubleshooting

| What you see | What to do |
|---|---|
| The coordinator cannot sign in | `ADMIN_PASSWORD` is still the default. Change it in `.env` and restart. |
| A case shows **Could not read** | The note beside it explains why, for example no axial CT series in the folder. Fix the folder and scan again. |
| The wrong series was used | Put part of the right series description in the `series` column of `cases.csv` and scan again. |
| Images load slowly | Set `MAX_INPLANE=256` in `.env`, restart and scan again. |
| Other computers cannot open the app | Set `HOST=0.0.0.0` and check the firewall allows the port. |

## Limits of this version

- Images are copied into `data/dicom`; the app does not query PACS directly.
- Enhanced multi-frame CT files are skipped; export as classic single-frame DICOM.
- CT only; MRI series are not imported.
- Scoring is by checklist; there are no drawing or measurement tools.

## Project structure

```
app/
  main.py            web server: sign-in, reader and coordinator APIs, statistics, export
  dicom_import.py    DICOM scanning, series selection, volume preparation
  blob_source.py     studies from Azure Blob Storage
  idrf.py            the 20 IDRF definitions
  db.py              SQLite schema
  config.py          settings from .env
  static/            reader, coordinator and sign-in pages, styles, fonts
tools/
  make_demo_data.py  synthetic CT phantoms for testing
  backup.py          safe database backup
run.py               starts the app
run.bat / run.sh     one-click start on Windows / Linux and macOS
```

## References

- Monclair T, et al. The International Neuroblastoma Risk Group (INRG) staging system: an INRG Task Force report. *J Clin Oncol.* 2009;27(2):298–303.
- Brisse HJ, et al. Guidelines for imaging and staging of neuroblastic tumors: consensus report from the INRG Project. *Radiology.* 2011;261(1):243–257.

## Licences of included components

Atkinson Hyperlegible font: SIL Open Font License (Braille Institute of America). Dependencies: FastAPI (MIT), Uvicorn (BSD), pydicom (MIT), NumPy (BSD), GDCM (BSD), Azure Storage Blob SDK (MIT). No licence is set for the application code itself; choose one before making the repository public.
