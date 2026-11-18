from app.extractors.research import extract_page_research


def test_extract_page_research_amazon():
    url = "https://www.amazon.in/Realme-Pro-Emerald-Green-Storage/dp/B0CX5ABCD"
    title = "Realme 15 Pro (Emerald Green, 256GB, 8GB RAM) - Amazon.in"
    text = """
    Realme 15 Pro 5G (Emerald Green, 256 GB) (8 GB RAM)
    Price: ₹24,999.00
    Sold by Appario Retail and Fulfilled by Amazon.
    In stock.
    """
    res = extract_page_research(url, title, text)
    assert res is not None
    assert res["domain"] == "amazon.in"
    assert "₹24,999" in res["price"]
    assert "256GB" in res["variant"] or "256 GB" in res["variant"]
    assert "Appario Retail" in res["seller"]
    assert res["in_stock"] is True


def test_extract_page_research_flipkart():
    url = "https://www.flipkart.com/realme-15-pro-5g-emerald-green-128-gb/p/itme12345"
    title = "Realme 15 Pro 5G (Emerald Green, 128 GB) Online at Best Price on Flipkart"
    text = """
    Realme 15 Pro 5G (Emerald Green, 128 GB)  (8 GB RAM)
    ₹22,999
    Seller: OmniTech Retail
    Free Delivery
    """
    res = extract_page_research(url, title, text)
    assert res is not None
    assert res["domain"] == "flipkart.com"
    assert "₹22,999" in res["price"]
    assert "128GB" in res["variant"] or "128 GB" in res["variant"]
    assert "OmniTech" in res["seller"]


def test_extract_page_research_ignores_search_engines():
    url = "https://www.google.com/search?q=realme+15+pro"
    title = "realme 15 pro - Google Search"
    text = "Realme 15 Pro price in India is ₹24,999 on Flipkart"
    res = extract_page_research(url, title, text)
    assert res is None



def test_extract_page_research_keeps_valid_og_image_and_rejects_unsafe_ones():
    """The product image comes from the page's og:image (collected by
    the extension). A valid CDN URL is kept — the image host is
    deliberately NOT the page host, because a store's product images
    live on its image CDN. Unsafe values are dropped, never trusted
    on arrival; no image at all yields None, not a placeholder."""
    url = "https://www.flipkart.com/samsung-galaxy-fit3/p/itm123"
    title = "Samsung Galaxy Fit3 Online at Best Price on Flipkart"
    text = "Samsung Galaxy Fit3\n₹3,899\nSeller: RetailNet\n"
    res = extract_page_research(
        url, title, text,
        image_url="https://rukminim2.flixcart.com/image/600/600/fit3.jpg")
    assert res is not None
    assert res["image"] == "https://rukminim2.flixcart.com/image/600/600/fit3.jpg"
    assert extract_page_research(url, title, text)["image"] is None
    for bad in ("javascript:alert(1)", "data:image/png;base64,AAAA",
                "https://user:pass@cdn.example.com/x.jpg",
                "ftp://cdn.example.com/x.jpg", ""):
        assert extract_page_research(url, title, text, image_url=bad)["image"] is None
