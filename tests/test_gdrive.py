"""Tests for the Google Drive mirror — pure helpers plus a fake Drive."""

import itertools
import re
from pathlib import Path

import pytest

from app.gdrive import (
    DOC_MIME,
    FOLDER_MIME,
    DriveMirror,
    checkboxes_to_symbols,
    page_identity,
    parse_folder_id,
    rewrite_html,
    split_front_matter,
)

pytest.importorskip("googleapiclient")
pytest.importorskip("markdown")

ROOT_ID = "RootFolder1234567890"


# --- pure helpers ------------------------------------------------------


@pytest.mark.parametrize("text", [
    "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp",
    "https://drive.google.com/drive/u/1/folders/1AbCdEfGhIjKlMnOp?usp=sharing",
    "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp",
    "  1AbCdEfGhIjKlMnOp ",
])
def test_parse_folder_id_accepts_urls_and_bare_ids(text):
    assert parse_folder_id(text) == "1AbCdEfGhIjKlMnOp"


@pytest.mark.parametrize("text", [
    "", "https://drive.google.com/drive/my-drive", "not an id", "https://x.com/",
])
def test_parse_folder_id_rejects_everything_else(text):
    assert parse_folder_id(text) == ""


def test_front_matter_is_split_from_the_body():
    meta, body = split_front_matter(
        '---\ntitle: "My page"\npage_id: "42"\nsource: https://c/x?a=1\n---\n\n# Hi\n'
    )
    assert meta == {"title": "My page", "page_id": "42", "source": "https://c/x?a=1"}
    assert body == "\n# Hi\n"


def test_no_front_matter_leaves_text_alone():
    assert split_front_matter("# Hi\n---\n") == ({}, "# Hi\n---\n")


def test_page_identity_prefers_the_confluence_page_id():
    assert page_identity("a/Title_123.md", {}) == ("page:123", "Title")
    assert page_identity("a/x.md", {"page_id": "9", "title": "T"}) == ("page:9", "T")


def test_page_identity_falls_back_to_a_bounded_path_key():
    key, title = page_identity("deep/" * 40 + "Plain.md", {})
    assert key.startswith("path:") and len(key) < 60
    assert title == "Plain"


def test_checkboxes_become_symbols():
    md = "- [x] done\n- [ ] todo\n  * [X] nested\nnot - [x] a list"
    assert checkboxes_to_symbols(md) == (
        "- ☑ done\n- ☐ todo\n  * ☑ nested\nnot - [x] a list"
    )


def test_rewrite_html_inlines_local_images_and_relinks_pages(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a b.png").write_bytes(b"\x89PNG")
    (tmp_path / "sub").mkdir()
    other = tmp_path / "Other_2.md"
    other.write_text("x")
    html = (
        '<img src="../images/a%20b.png"><img src="https://wiki/x.png">'
        '<a href="../Other_2.md#Section">o</a><a href="https://example.com">e</a>'
        '<a href="../../outside.md">out</a>'
    )
    out, remote = rewrite_html(
        html, tmp_path / "sub", tmp_path,
        lambda p: "https://docs/2" if p == other.resolve() else "",
    )
    assert 'src="data:image/png;base64,iVBORw=="' in out
    assert 'href="https://docs/2"' in out
    assert 'href="https://example.com"' in out
    assert 'href="../../outside.md"' in out
    assert remote == 1


# --- fake Drive ----------------------------------------------------------


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class FakeDrive:
    """Just enough of the Drive v3 ``files`` resource for DriveMirror."""

    def __init__(self, drive_id="SharedDrive1"):
        self.items = {ROOT_ID: {"id": ROOT_ID, "name": "Target",
                                "mimeType": FOLDER_MIME, "parents": [],
                                "appProperties": {}}}
        self.content: dict[str, bytes] = {}
        self.uploads = 0
        self.drive_id = drive_id
        self._ids = (f"id{n}" for n in itertools.count())

    def files(self):
        return self

    def get(self, fileId, **_):
        item = dict(self.items[fileId])
        item["driveId"] = self.drive_id
        item["capabilities"] = {"canAddChildren": True}
        return _Call(lambda: item)

    def list(self, q, pageToken=None, **kwargs):
        assert kwargs.get("driveId") == self.drive_id
        root = re.search(r"value='([^']+)'", q).group(1)
        found = [dict(i) for i in self.items.values()
                 if (i.get("appProperties") or {}).get("l33chRoot") == root]
        return _Call(lambda: {"files": found})

    def create(self, body, media_body=None, **_):
        new_id = next(self._ids)
        self.items[new_id] = {"id": new_id, "name": body["name"],
                              "mimeType": body.get("mimeType", "file"),
                              "parents": body["parents"],
                              "appProperties": dict(body.get("appProperties", {}))}
        if media_body is not None:
            self._store(new_id, media_body)
        return _Call(lambda: {"id": new_id})

    def update(self, fileId, body=None, media_body=None, addParents=None,
               removeParents=None, **_):
        item = self.items[fileId]
        body = body or {}
        if "name" in body:
            item["name"] = body["name"]
        item["appProperties"].update(body.get("appProperties", {}))
        if addParents:
            item["parents"] = [addParents]
        if media_body is not None:
            self._store(fileId, media_body)
        return _Call(lambda: {"id": fileId})

    def _store(self, file_id, media):
        self.uploads += 1
        self.content[file_id] = media.getbytes(0, media.size())

    # helpers for assertions
    def by_name(self, name, mime=None):
        matches = [i for i in self.items.values() if i["name"] == name
                   and (mime is None or i["mimeType"] == mime)]
        assert len(matches) == 1, f"{name}: {matches}"
        return matches[0]

    def html(self, name):
        return self.content[self.by_name(name, DOC_MIME)["id"]].decode("utf-8")


def _export(root: Path) -> None:
    (root / "Parent").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "files").mkdir()
    (root / "images" / "pic.png").write_bytes(b"\x89PNG")
    (root / "files" / "spec.pdf").write_bytes(b"%PDF")
    (root / "Parent" / "Parent_1.md").write_text(
        "# Parent\n\n- [x] done\n\nSee [child](Child_2.md) and "
        "[spec](../files/spec.pdf).\n\n![pic](../images/pic.png)\n",
        encoding="utf-8",
    )
    (root / "Parent" / "Child_2.md").write_text(
        "# Child\n\nBack to [parent](Parent_1.md).\n", encoding="utf-8"
    )
    (root / "README.md").write_text(
        "- [Parent](Parent/Parent_1.md)\n", encoding="utf-8"
    )
    (root / ".l33ch-state.json").write_text("{}")


def _mirror(drive, root):
    return DriveMirror(drive, ROOT_ID, root).run()


def test_first_run_mirrors_folders_docs_links_and_images(tmp_path):
    _export(tmp_path)
    drive = FakeDrive()
    stats = _mirror(drive, tmp_path)

    assert (stats.created, stats.updated, stats.failed) == (3, 0, 0)
    parent_folder = drive.by_name("Parent", FOLDER_MIME)
    assert parent_folder["mimeType"] == FOLDER_MIME
    assert parent_folder["parents"] == [ROOT_ID]

    parent_doc = drive.by_name("Parent", DOC_MIME)
    child_doc = drive.by_name("Child")
    assert parent_doc["mimeType"] == DOC_MIME
    assert parent_doc["parents"] == [parent_folder["id"]]
    assert parent_doc["appProperties"]["l33chKey"] == "page:1"

    html = drive.html("Parent")
    assert f"https://docs.google.com/document/d/{child_doc['id']}/edit" in html
    assert "data:image/png;base64," in html
    assert "☑ done" in html
    spec = drive.by_name("spec.pdf")
    assert f"https://drive.google.com/file/d/{spec['id']}/view" in html
    assert drive.by_name("files")["parents"] == [ROOT_ID]

    assert drive.by_name("README")["parents"] == [ROOT_ID]
    assert not [i for i in drive.items.values() if i["name"].startswith(".l33ch")]


def test_rerun_without_changes_uploads_nothing(tmp_path):
    _export(tmp_path)
    drive = FakeDrive()
    _mirror(drive, tmp_path)
    uploads, count = drive.uploads, len(drive.items)

    stats = _mirror(drive, tmp_path)
    assert (stats.created, stats.updated, stats.unchanged) == (0, 0, 3)
    assert drive.uploads == uploads
    assert len(drive.items) == count


def test_changed_page_updates_the_same_doc_in_place(tmp_path):
    _export(tmp_path)
    drive = FakeDrive()
    _mirror(drive, tmp_path)
    child_id = drive.by_name("Child")["id"]

    (tmp_path / "Parent" / "Child_2.md").write_text("# Child\n\nNew text.\n")
    stats = _mirror(drive, tmp_path)

    assert (stats.created, stats.updated, stats.unchanged) == (0, 1, 2)
    assert drive.by_name("Child")["id"] == child_id
    assert "New text." in drive.html("Child")


def test_renamed_and_moved_page_keeps_its_doc(tmp_path):
    _export(tmp_path)
    drive = FakeDrive()
    _mirror(drive, tmp_path)
    child_id = drive.by_name("Child")["id"]

    (tmp_path / "Parent" / "Child_2.md").rename(tmp_path / "Renamed_2.md")
    _mirror(drive, tmp_path)

    doc = drive.items[child_id]
    assert doc["name"] == "Renamed"
    assert doc["parents"] == [ROOT_ID]
    assert len([i for i in drive.items.values() if i["mimeType"] == DOC_MIME]) == 3


def test_items_not_created_by_the_mirror_are_ignored(tmp_path):
    _export(tmp_path)
    drive = FakeDrive()
    drive.items["manual"] = {"id": "manual", "name": "Child", "mimeType": DOC_MIME,
                             "parents": [ROOT_ID], "appProperties": {}}
    _mirror(drive, tmp_path)
    assert "manual" not in drive.content
    assert drive.items["manual"]["name"] == "Child"


def test_links_to_docs_created_later_in_the_run_are_filled_in(tmp_path):
    # README and Child sort before Parent, so their links point ahead.
    _export(tmp_path)
    drive = FakeDrive()
    _mirror(drive, tmp_path)
    parent_url = (
        f"https://docs.google.com/document/d/"
        f"{drive.by_name('Parent', DOC_MIME)['id']}/edit"
    )
    assert parent_url in drive.html("README")
    assert parent_url in drive.html("Child")
    assert ".md" not in drive.html("Child")


def test_a_cancelled_run_leaves_no_empty_docs(tmp_path):
    _export(tmp_path)
    drive = FakeDrive()
    calls = iter([False, True])
    DriveMirror(drive, ROOT_ID, tmp_path, cancelled=lambda: next(calls, True)).run()

    docs = [i for i in drive.items.values() if i["mimeType"] == DOC_MIME]
    assert len(docs) == 1
    assert all(drive.content.get(d["id"]) for d in docs)


def test_empty_docs_from_an_interrupted_older_run_get_filled(tmp_path):
    # The previous version created every Doc empty first; a cancelled run
    # left those behind, tagged but without a content hash.
    _export(tmp_path)
    drive = FakeDrive()
    drive.items["old"] = {"id": "old", "name": "Child", "mimeType": DOC_MIME,
                          "parents": [ROOT_ID],
                          "appProperties": {"l33chRoot": ROOT_ID,
                                            "l33chKey": "page:2"}}
    stats = _mirror(drive, tmp_path)

    assert (stats.created, stats.updated) == (2, 1)
    assert "Back to" in drive.content["old"].decode("utf-8")
    assert drive.items["old"]["parents"] == [drive.by_name("Parent", FOLDER_MIME)["id"]]
