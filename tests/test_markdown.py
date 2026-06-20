from app.services.markdown import render_markdown


def test_renders_basic_markdown():
    html = render_markdown("**bold** and `code`")
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_strips_dangerous_html():
    html = render_markdown("hi <script>alert(1)</script> <img src=x onerror=alert(2)>")
    assert "<script>" not in html and "alert(1)" not in html
    assert "onerror" not in html


def test_empty():
    assert render_markdown(None) == ""
    assert render_markdown("") == ""
