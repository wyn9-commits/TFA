from tfa_core.adapters.sharepoint import SharePointSource


def _raw(name, parent="/drive/root:/2026_08_11_batch1", deleted=False, folder=False):
    d = {
        "id": "item1",
        "eTag": "\"v1\"",
        "name": name,
        "size": 123,
        "parentReference": {"path": parent},
        "createdBy": {"user": {"displayName": "Jane Doe"}},
        "@microsoft.graph.downloadUrl": "https://example/dl",
    }
    if deleted:
        d["deleted"] = {"state": "deleted"}
    if folder:
        d["folder"] = {}
    return d


def _parse(raw):
    return SharePointSource.__new__(SharePointSource)._parse(raw)


def test_pdf_in_identifier_folder_parses():
    item = _parse(_raw("folio.pdf"))
    assert item is not None
    assert item.identifier == "2026_08_11_batch1"
    assert item.uploader == "Jane Doe"


def test_nested_subfolder_maps_to_top_identifier():
    item = _parse(_raw("folio.pdf", parent="/drive/root:/batch2/argentina/hilton"))
    assert item.identifier == "batch2"


def test_folders_and_unsupported_types_skipped():
    assert _parse(_raw("notes.docx")) is None
    assert _parse(_raw("sub", folder=True)) is None


def test_root_level_file_skipped():
    assert _parse(_raw("loose.pdf", parent="/drive/root:")) is None


def test_deleted_item_flagged():
    item = _parse(_raw("folio.pdf", deleted=True))
    assert item.deleted is True
