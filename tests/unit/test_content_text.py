from app.services.content_text import condense_image_descriptions, strip_image_lines


def test_dated_stamp_is_kept_compactly():
    text = "Kopf\n![Official stamp of the Amtsgericht Ingolstadt dated 28. AUG. 2026]()An official rectangular stamp.\nText"
    assert condense_image_descriptions(text) == (
        "Kopf\n[Bild: Official stamp of the Amtsgericht Ingolstadt dated 28. AUG. 2026]\nText"
    )


def test_logo_without_digits_is_dropped():
    text = "![Crest of the Free State of Bavaria]()A heraldic crest.\nBetreff"
    assert condense_image_descriptions(text) == "\nBetreff"


def test_narration_after_markup_is_dropped():
    text = "![Stamp 28.08.2026]()Red stamp, handwritten date.\nweiter"
    assert "Red stamp" not in condense_image_descriptions(text)


def test_text_without_images_is_unchanged():
    text = "Sehr geehrte Damen und Herren,\n\nin der Sache [1] wird mitgeteilt."
    assert condense_image_descriptions(text) == text


def test_strip_image_lines_removes_the_whole_line():
    assert strip_image_lines("a ![x 1]()narration\nb").split() == ["a", "b"]
