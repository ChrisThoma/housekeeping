import json
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import ai, db, documents


def make_pdf(pages: list[str]) -> bytes:
    """Minimal valid PDF with one line of text per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{o}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


NOTES = ai.DocNotes(
    document_type="Owner's manual", model_identified="Carrier 59SC5", summary="Furnace care.",
    maintenance=[ai.ManualItem(task="Replace filter", frequency="every 3 months", details="Use a 16x25x1 filter.", page="34")],
    parts=[ai.Part(name="Air filter", spec="16x25x1 MERV 8")], warnings=[],
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    from app import main
    db.init()
    return TestClient(main.app)


def test_sniff_and_html_extraction():
    assert documents.sniff_mime(b"%PDF-1.7 ...") == "application/pdf"
    assert documents.sniff_mime(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert documents.sniff_mime(b"<!DOCTYPE html><html>", "text/html") == "text/html"
    assert documents.sniff_mime(b"\x00\x01\x02\xff\xfe", "application/zip") is None
    text, title = documents.html_to_text(b"<html><head><title>Manual</title><script>x=1</script></head><body><h1>Care</h1><p>Clean coils yearly.</p></body></html>")
    assert title == "Manual" and "Clean coils yearly." in text and "x=1" not in text


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.pdf", "http://192.168.1.1/admin", "http://localhost:8000/", "file:///etc/passwd", "ftp://example.com/a.pdf"])
def test_download_refuses_local_and_odd_urls(url):
    with pytest.raises(documents.DocumentError):
        documents.download(url)


def test_pdf_info_reads_text():
    pages, text = documents.pdf_info(make_pdf(["Change filter every 90 days", "Clean drain pan"]))
    assert pages == 2 and "[Page 2]" in text and "Clean drain pan" in text


def test_upload_read_and_use_in_suggestions(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "read_document", lambda doc, path, asset: (NOTES, "full", "file_abc"))
    def fake_suggest(asset, existing, settings, today, doc_notes=None):
        seen["notes"] = doc_notes
        return []
    monkeypatch.setattr(ai, "suggest_for_asset", fake_suggest)

    asset = client.post("/api/assets", json={"category": "home", "name": "Furnace"}).json()
    r = client.post("/api/documents/upload", data={"asset_id": str(asset["id"])},
                    files={"file": ("furnace.pdf", make_pdf(["Replace filter every 3 months"]), "application/pdf")}).json()
    doc = r["document"]
    assert r["read_error"] is None and doc["pages"] == 1 and doc["read_mode"] == "full"
    assert doc["notes"]["parts"][0]["spec"] == "16x25x1 MERV 8" and doc["anthropic_file_id"] == "file_abc"

    # The stored file is served back for viewing.
    f = client.get(f"/api/documents/{doc['id']}/file")
    assert f.status_code == 200 and f.content.startswith(b"%PDF")

    client.post(f"/api/assets/{asset['id']}/suggest")
    assert seen["notes"][0][0] == "furnace" and seen["notes"][0][1]["maintenance"][0]["page"] == "34"
    assert client.get("/api/assets").json()[0]["doc_count"] == 1


def test_document_kept_when_reading_fails(client, monkeypatch):
    def fail(*a):
        raise RuntimeError("Claude declined this request.")
    monkeypatch.setattr(ai, "read_document", fail)
    r = client.post("/api/documents/upload", files={"file": ("guide.txt", b"Monthly: test GFCI outlets\n" * 20, "text/plain")}).json()
    assert r["read_error"] == "Claude declined this request." and r["document"]["notes"] is None
    assert len(client.get("/api/documents").json()) == 1


def test_upload_rejects_unknown_types(client):
    r = client.post("/api/documents/upload", files={"file": ("a.zip", b"PK\x03\x04\x00\xff\xfe", "application/zip")})
    assert r.status_code == 400


def test_find_documents_downloads_and_reads(client, monkeypatch):
    monkeypatch.setattr(ai, "find_documents", lambda asset, settings: [
        ai.FoundDocument(title="CR-V Owner's Manual", url="https://example.com/crv.pdf", kind="manual", why="Official"),
        ai.FoundDocument(title="Broken link", url="https://example.com/missing.pdf", kind="manual", why="Maybe"),
    ])
    def fake_download(url):
        if "missing" in url:
            raise documents.DocumentError("The site returned an error (404) for that link.")
        return make_pdf(["Oil every 7,500 miles"]), "application/pdf", "crv.pdf"
    monkeypatch.setattr(documents, "download", fake_download)
    monkeypatch.setattr(ai, "read_document", lambda doc, path, asset: (NOTES, "full", None))

    car = client.post("/api/assets", json={"category": "vehicle", "name": "CR-V", "year": 2019}).json()
    r = client.post(f"/api/assets/{car['id']}/find-documents").json()
    assert r["found"] == 2 and [d["title"] for d in r["added"]] == ["CR-V Owner's Manual"]
    assert "404" in r["skipped"][0]["reason"]
    # Running it again doesn't save duplicates.
    r = client.post(f"/api/assets/{car['id']}/find-documents").json()
    assert not r["added"] and r["skipped"][0]["reason"] == "Already saved"

    # Deleting the item removes its files from disk.
    path = documents.docs_dir() / client.get("/api/documents").json()[0]["filename"]
    assert path.exists()
    client.delete(f"/api/assets/{car['id']}")
    assert not path.exists() and client.get("/api/documents").json() == []


def test_found_documents_without_care_instructions_are_dropped(client, monkeypatch):
    monkeypatch.setattr(ai, "find_documents", lambda asset, settings: [
        ai.FoundDocument(title="Spec sheet", url="https://example.com/spec.pdf", kind="other", why="Official"),
        ai.FoundDocument(title="Use and Care", url="https://example.com/care.pdf", kind="manual", why="Official"),
    ])
    monkeypatch.setattr(documents, "download", lambda url: (make_pdf(["x"]), "application/pdf", url.rsplit("/", 1)[1]))
    spec = NOTES.model_copy(update={"document_type": "Spec sheet", "maintenance": []})
    forgotten = []
    monkeypatch.setattr(ai, "read_document", lambda doc, path, asset: (spec if "spec" in doc["filename"] else NOTES, "full", f"file_{doc['id']}"))
    monkeypatch.setattr(ai, "forget_file", forgotten.append)

    dw = client.post("/api/assets", json={"category": "appliance", "name": "Dishwasher"}).json()
    r = client.post(f"/api/assets/{dw['id']}/find-documents").json()
    assert [d["title"] for d in r["added"]] == ["Use and Care"]
    assert r["skipped"][0]["reason"] == "no maintenance instructions (Spec sheet)"
    saved = client.get("/api/documents").json()
    assert [d["title"] for d in saved] == ["Use and Care"]
    assert len(list(documents.docs_dir().iterdir())) == 1 and forgotten == ["file_1"]


def test_long_pdfs_are_read_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "FULL_READ_MAX_PAGES", 2)
    uploads = []
    monkeypatch.setattr(ai, "client", lambda: SimpleNamespace(files=SimpleNamespace(upload=lambda file: uploads.append(file) or SimpleNamespace(id="file_1"))))

    short = tmp_path / "short.pdf"
    short.write_bytes(make_pdf(["a", "b"]))
    block, mode, fid = ai.document_block({"mime": "application/pdf", "title": "Short", "filename": "short.pdf", "pages": 2}, short)
    assert mode == "full" and block["source"] == {"type": "file", "file_id": "file_1"} and fid == "file_1"

    long = tmp_path / "long.pdf"
    long.write_bytes(make_pdf(["Filter every 90 days", "b", "c"]))
    block, mode, _ = ai.document_block({"mime": "application/pdf", "title": "Long", "filename": "long.pdf", "pages": 3}, long)
    assert mode == "text" and "Filter every 90 days" in block["source"]["data"]
    assert len(uploads) == 1


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_scan_fills_in_an_item_and_keeps_the_label(client, monkeypatch):
    scan = ai.ScannedItem(readable=True, problem=None, category="appliance", name="Dishwasher", make="Bosch",
                          model="SHXM78Z55N/18", serial="FD 9907", year=2019, year_basis="Bosch FD 9907 = July 2019",
                          mileage=12, specs=[ai.Part(name="Rinse aid", spec="setting r:05")], care=[])
    monkeypatch.setattr(ai, "scan_item", lambda data, mime: scan)
    r = client.post("/api/assets/scan", files={"file": ("label.png", PNG, "image/png")}).json()
    assert r["details"] == "Bosch SHXM78Z55N/18" and r["year"] == 2019 and r["category"] == "appliance"
    assert r["mileage"] is None  # only vehicles have an odometer
    assert r["notes"] == "Serial FD 9907 · Rinse aid: setting r:05"
    assert "serial FD 9907" in r["label_notes"]["model_identified"]

    asset = client.post("/api/assets", json={"category": "appliance", "name": "Dishwasher", "details": r["details"]}).json()
    up = client.post("/api/documents/upload", files={"file": ("label.png", PNG, "image/png")},
                     data={"asset_id": asset["id"], "kind": "photo", "notes": json.dumps(r["label_notes"])}).json()
    assert up["read_error"] is None
    assert up["document"]["notes"]["document_type"] == "Data plate photo"
    assert up["document"]["notes"]["parts"][0]["spec"] == "setting r:05"


def test_scan_rejects_unreadable_photos_and_non_images(client, monkeypatch):
    blurry = ai.ScannedItem(readable=False, problem="Too much glare on the label.", category="appliance", name="",
                            make=None, model=None, serial=None, year=None, year_basis=None, mileage=None, specs=[], care=[])
    monkeypatch.setattr(ai, "scan_item", lambda data, mime: blurry)
    r = client.post("/api/assets/scan", files={"file": ("label.png", PNG, "image/png")})
    assert r.status_code == 422 and r.json()["detail"] == "Too much glare on the label."
    assert client.post("/api/assets/scan", files={"file": ("m.pdf", make_pdf(["x"]), "application/pdf")}).status_code == 400
    assert client.get("/api/assets").json() == []
