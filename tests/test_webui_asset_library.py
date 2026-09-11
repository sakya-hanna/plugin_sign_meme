"""浅色模板资产库的静态页面契约。"""
from pathlib import Path


PAGE = (
    Path(__file__).resolve().parents[1]
    / "pages"
    / "举牌模板"
    / "index.html"
)


def test_asset_library_controls_and_empty_state_are_present():
    html = PAGE.read_text(encoding="utf-8")

    for marker in (
        'id="template-search"',
        'id="template-status-filter"',
        'id="template-sort"',
        'id="template-result-count"',
        'id="template-empty-state"',
        'id="clear-template-filters"',
    ):
        assert marker in html


def test_asset_library_filters_local_template_metadata_without_native_modals():
    html = PAGE.read_text(encoding="utf-8")

    assert "applyTemplateView" in html
    assert "updated_at" in html
    assert "localeCompare" in html
    assert "confirmBox(" in html
    assert "window.confirm(" not in html
    assert "window.alert(" not in html
    assert "window.prompt(" not in html


def test_template_preview_has_a_full_image_dialog_contract():
    html = PAGE.read_text(encoding="utf-8")

    assert "template-preview-button" in html
    assert 'data-full-preview-id=' in html
    assert "openImagePreview" in html
    assert "dialog-image" in html
    assert 'id="image-preview-dialog"' in html
    assert '#image-preview-dialog[hidden] { display:none; }' in html
    # Asset-library thumbnails must scale the entire source image into the card,
    # independently from the click-to-open full-image dialog.
    assert '.template-preview-frame { width:100%; height:100%; display:flex; align-items:center; justify-content:center; overflow:hidden; }' in html
    assert '.template-preview img { display:block !important; width:auto !important; height:auto !important; max-width:100% !important; max-height:100% !important; object-fit:contain !important; object-position:center !important; }' in html
