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

