# Experimental visual listing reader

Manual Import Listings uses the original reader first, then runs this reader
once if important fields are unreadable. Successful and empty primary fields
are preserved. Missing colours alone never trigger recovery. Automatic background
imports keep the original reader. Desktop releases include the OCR runtime.
Import reports are saved in the app data folder under `visual-listing-reader`.
The standalone test
script still does not write listing records. All proposed field values require
manual review; missing fields and conflicts remain explicit.

The project automatically discovers `.ocr_runtime/tesseract.exe`. You can also
set `TESSERACT_PATH` or put Tesseract on PATH. English language data must be
installed. No cloud account is needed.

From the project terminal with the normal Python dependencies available:

```powershell
$env:TESSERACT_PATH = 'C:\Program Files\Tesseract-OCR\tesseract.exe'
python tools/test_visual_listing_reader.py --account 'Saved account title' --edit-url 'https://www.facebook.com/marketplace/edit/?listing_id=LISTING_ID' --download-images
```

Results are written under `outputs/visual-listing-reader`: screenshots, OCR
lines, field proposals, image/video URLs, and `report.json`. Optional image
downloads use the existing original-byte downloader in an isolated test folder.
These files contain private listing information and are excluded from Git.

For an existing driver already on an edit page:

```python
from automation.visual_listing_reader import read_listing_edit_visual
result = read_listing_edit_visual(driver, 'outputs/visual-test')
```

The reader scrolls the edit panel with overlap and restores its original scroll
position. It uses screenshot OCR for field suggestions and reads actual form
controls on every capture to resolve OCR errors, checkbox state, and empty
values. Reports retain OCR evidence and flag disagreements with control values.
Fields absent from the form remain explicitly unreadable; an empty description
is recorded as empty rather than populated from helper text. It uses the existing DOM
image extractor for photos. Video extraction reads `<video>` and `<source>`
URLs. Direct Facebook CDN URLs are returned; blob, unsupported, and streaming
sources are flagged. It does not download videos, intercept network traffic, or
recover hidden media. OCR cannot reliably recover offscreen, truncated, or
multiline values in every layout. Inspect screenshots and conflicts before use.

An optional `vision(path)` callback may be supplied later; it returns a mapping
of field names to text suggestions. No vision provider is currently configured.
