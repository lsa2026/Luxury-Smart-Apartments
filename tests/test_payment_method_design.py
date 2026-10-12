"""Design boundaries that must not change the hosted payment workflow."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scoped_design_preserves_provider_button_and_hidden_methods():
    css = (ROOT / "static/css/checkout-methods.css").read_text(encoding="utf-8")
    template = (ROOT / "templates/payments/hyperpay_checkout.html").read_text(encoding="utf-8")
    base = (ROOT / "templates/base.html").read_text(encoding="utf-8")
    assert "checkout-methods.css" not in base
    assert "checkout-methods.css" in template
    assert "--apple-pay-button-width: 100%" in css
    assert "--apple-pay-button-height: 54px" in css
    assert "--apple-pay-button-height: 49px" in css
    assert "@container (max-width: 560px)" in css
    assert "min-height: 90px" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css
    assert ":not([hidden])" in css
    assert "appearance:" not in css
    assert "<apple-pay-button" not in template
    assert "apple-pay-button-label" not in template
    for asset in ("visa.svg", "master.svg", "apple-pay-mark.svg", "hyperpay-brand-sprite.png"):
        assert (ROOT / "static/images/payment" / asset).is_file()
    for font in ("cairo-arabic.woff2", "inter-latin.woff2"):
        assert (ROOT / "static/fonts/checkout" / font).read_bytes().startswith(b"wOF2")
