"""Unit tests for Multi-Retailer Product Research, Spec Extraction, and Comparison."""
from __future__ import annotations

from app.research.collector import classify_claim_type
from app.research.product_extractor import (
    compare_product_listings,
    extract_product_listings,
    parse_price,
)


def test_classify_claim_type():
    """Verify categorization of statements into claim types."""
    assert classify_claim_type("A transformer is defined as a deep learning architecture using self-attention.") == "definition"
    assert classify_claim_type("The multi-head attention mechanism computes scaled dot-product attention in parallel.") == "mechanism"
    assert classify_claim_type("FlashAttention achieves a 3x to 5x speedup and significantly outperforms standard attention.") == "advantage"
    assert classify_claim_type("The primary limitation is quadratic memory complexity which causes an attention bottleneck.") == "disadvantage"
    assert classify_claim_type("The model has 175B parameters and achieves 99.2% accuracy with 15ms latency.") == "statistic"
    assert classify_claim_type("The project repository was released under the Apache 2.0 license.") == "general"


def test_parse_price_currencies():
    """Verify price parsing across INR and USD formats."""
    fmt_inr, val_inr, curr_inr = parse_price("Now available for ₹79,990 with free shipping")
    assert fmt_inr == "₹79,990"
    assert val_inr == 79990.0
    assert curr_inr == "INR"

    fmt_usd, val_usd, curr_usd = parse_price("Discounted price: $1,249.99 today")
    assert fmt_usd == "$1,249.99"
    assert val_usd == 1249.99
    assert curr_usd == "USD"


def test_multi_retailer_product_extraction():
    """Verify extracting structured specifications and variants across retailers."""
    amazon_text = """
    Acer Nitro V Gaming Laptop (13th Gen Intel Core i5-13420H / RTX 4050 6GB / 16 GB DDR5 / 512 GB SSD / 144Hz)
    ₹72,990 M.R.P: ₹92,999 (22% off)
    4.2 out of 5 stars
    """
    amz_listings = extract_product_listings(amazon_text, domain="amazon.in", url="https://www.amazon.in/dp/B0CX")
    assert len(amz_listings) >= 1
    item = amz_listings[0]
    assert "Acer Nitro V" in item["title"]
    assert item["price"] == "₹72,990"
    assert item["price_num"] == 72990.0
    assert item["gpu"] == "RTX 4050"
    assert "i5-13420H" in item["cpu"]
    assert "16 GB" in item["ram"]
    assert "512 GB" in item["storage"]
    assert item["domain"] == "amazon.in"

    flipkart_text = """
    Acer Nitro V Intel Core i5 13th Gen 13420H - (16 GB/512 GB SSD/Windows 11 Home/6 GB Graphics/NVIDIA GeForce RTX 4050) ANV15-51
    ₹74,990
    4.4 ★ Ratings
    """
    fk_listings = extract_product_listings(flipkart_text, domain="flipkart.com", url="https://www.flipkart.com/acer-nitro")
    assert len(fk_listings) >= 1
    fk_item = fk_listings[0]
    assert fk_item["price_num"] == 74990.0
    assert fk_item["domain"] == "flipkart.com"


def test_cross_retailer_comparison_and_best_price():
    """Verify multi-retailer comparison table generation and claim synthesis."""
    listings = [
        {
            "title": "Acer Nitro V (RTX 4050, 16GB, 512GB)",
            "domain": "amazon.in",
            "url": "https://www.amazon.in/dp/1",
            "price": "₹72,990",
            "price_num": 72990.0,
            "currency": "INR",
            "cpu": "Intel Core i5-13420H",
            "gpu": "RTX 4050",
            "ram": "16 GB",
            "storage": "512 GB SSD",
            "display": "15.6 inch",
        },
        {
            "title": "Acer Nitro V (RTX 4050, 16GB, 512GB)",
            "domain": "flipkart.com",
            "url": "https://www.flipkart.com/dp/2",
            "price": "₹74,990",
            "price_num": 74990.0,
            "currency": "INR",
            "cpu": "Intel Core i5-13420H",
            "gpu": "RTX 4050",
            "ram": "16 GB",
            "storage": "512 GB SSD",
            "display": "15.6 inch",
        },
    ]

    table, claims = compare_product_listings(listings, topic_title="Gaming Laptop Comparison")
    assert len(table.rows) == 2
    assert table.headers[0] == "Product / Model"

    # Row 0 must be lowest price
    assert table.rows[0][1] == "amazon.in"
    assert table.rows[0][2] == "₹72,990"
    assert "Best Price" in table.rows[0][7]

    # Synthesized claims
    assert len(claims) >= 2
    assert any("lowest price" in c.statement.lower() for c in claims)
    assert any("flipkart.com" in c.statement.lower() or "amazon.in" in c.statement.lower() for c in claims)


def test_parse_emi_details_variations():
    """Verify robust extraction of EMI terms across various retail formats."""
    from app.research.product_extractor import parse_emi_details

    has_emi, details = parse_emi_details("No Cost EMI starts at ₹3,889/month on select credit cards")
    assert has_emi is True
    assert "No Cost EMI" in details
    assert "₹3,889/month" in details

    has_emi2, details2 = parse_emi_details("Standard EMI starts at ₹2,450/mo with zero down payment")
    assert has_emi2 is True
    assert "EMI starts at" in details2
    assert "₹2,450/mo" in details2

    has_emi3, details3 = parse_emi_details("Interest-free EMI available for 6 months")
    assert has_emi3 is True
    assert "EMI" in details3

    has_emi4, details4 = parse_emi_details("Free express delivery and standard 1-year manufacturer warranty")
    assert has_emi4 is False
    assert details4 == "Not advertised"


def test_smartphone_product_extraction_and_emi():
    """Verify category-independent extraction for smartphones (Samsung Galaxy S25 5G) with EMI and specs."""
    sample_text = """
    Samsung Galaxy S25 5G (Silver Shadow, 12 GB RAM, 256 GB Storage) | Snapdragon 8 Elite | 50 MP Triple Camera
    ₹74,999 M.R.P: ₹82,999 (10% off)
    No Cost EMI starts at ₹3,889/month on select bank cards
    4.6 out of 5 stars
    """
    listings = extract_product_listings(sample_text, domain="amazon.in", url="https://www.amazon.in/dp/B0S25")
    assert len(listings) >= 1
    item = listings[0]
    assert "Samsung Galaxy S25" in item["title"]
    assert item["price"] == "₹74,999"
    assert item["price_num"] == 74999.0
    assert item["category"] == "smartphone"
    assert item["emi_available"] is True
    assert "No Cost EMI" in item["emi_details"]
    assert "₹3,889/month" in item["emi_details"]
    assert item["domain"] == "amazon.in"
    assert item["url"] == "https://www.amazon.in/dp/B0S25"
    assert "12 GB" in item["ram"]
    assert "256 GB" in item["storage"]
    assert "50 MP" in item["specs"]["camera"]
    assert item["specs"]["cpu"] == "Snapdragon 8 Elite"
    assert "date_checked" in item


def test_smartphone_cross_retailer_comparison_with_emi():
    """Verify multi-retailer smartphone comparison table and claim synthesis with EMI details."""
    listings = [
        {
            "title": "Samsung Galaxy S25 5G (12GB RAM, 256GB)",
            "domain": "amazon.in",
            "url": "https://www.amazon.in/dp/B0S25",
            "price": "₹74,999",
            "price_num": 74999.0,
            "currency": "INR",
            "category": "smartphone",
            "emi_available": True,
            "emi_details": "No Cost EMI starts at ₹3,889/month",
            "specs_summary": "Snapdragon 8 Elite / 12 GB RAM / 256 GB / 50 MP Camera",
            "date_checked": "2026-11-12",
        },
        {
            "title": "Samsung Galaxy S25 5G (12GB RAM, 256GB)",
            "domain": "samsung.com",
            "url": "https://www.samsung.com/in/smartphones/galaxy-s25",
            "price": "₹76,999",
            "price_num": 76999.0,
            "currency": "INR",
            "category": "smartphone",
            "emi_available": True,
            "emi_details": "No Cost EMI starts at ₹3,999/month",
            "specs_summary": "Snapdragon 8 Elite / 12 GB RAM / 256 GB / 50 MP Camera",
            "date_checked": "2026-11-12",
        },
    ]

    table, claims = compare_product_listings(listings, topic_title="Samsung Galaxy S25 Comparison")
    assert len(table.rows) == 2
    assert table.headers == ["Product Name", "Retailer", "Price", "EMI Availability", "EMI Details", "Key Specifications", "Product Link", "Deal Evaluation"]

    # Row 0 is lowest price on amazon.in
    assert table.rows[0][1] == "amazon.in"
    assert table.rows[0][2] == "₹74,999"
    assert table.rows[0][3] == "Available"
    assert "No Cost EMI" in table.rows[0][4]
    assert "⭐ Best Price" in table.rows[0][7]

    # Claims verification
    assert len(claims) >= 2
    assert any("lowest price" in c.statement.lower() for c in claims)
    assert any("₹74,999" in c.statement for c in claims)
    assert any("emi" in c.statement.lower() for c in claims)


