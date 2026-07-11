"""Shared fixtures for the CleanFrame test suite."""

from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture
def messy_df() -> pd.DataFrame:
    """A representative messy customer table exercising most detectors."""
    return pd.DataFrame(
        {
            "Customer Name": ["  alice smith ", "BOB JONES", "charlie brown", "alice smith", "Dan Adams", "Eve Lin"],
            "Signup Date": ["31/01/2024", "2024-02-15", "1 Jan 2024", "15/03/2024", "05/06/2024", "20/07/2024"],
            "Amount": ["₹1,20,000", "₹1,200", "₹1200", "₹50,000", "₹0", "₹9,999"],
            "City": ["Bengaluru", "bengaluru ", "Mumbai", "MUMBAI", "Bengaluru", "Delhi"],
            "Email": ["A@X.com ", "bob@y.org", "not-an-email", "dan@z.co", "e@w.io", "f@w.io"],
            "Phone": ["+91 98765 43210", "9876543210", "(080) 1234-5678", "080 22334455", "N/A", "9000011111"],
        }
    )


@pytest.fixture
def drifted_df() -> pd.DataFrame:
    """Next month's file: Amount renamed, a new date format, a duplicate row."""
    return pd.DataFrame(
        {
            "Customer Name": ["Frank Ray", "grace lee", "Frank Ray"],
            "Signup Date": ["Jan 5, 2026", "2026-02-01", "Jan 5, 2026"],
            "Amt (INR)": ["₹2,00,000", "₹3,000", "₹2,00,000"],
            "City": ["Bengaluru", "chennai", "Bengaluru"],
            "Email": ["frank@a.com", "grace@b.com", "frank@a.com"],
            "Phone": ["+91 90000 11111", "9111122222", "+91 90000 11111"],
        }
    )


@pytest.fixture
def clean_df() -> pd.DataFrame:
    """An already-tidy frame, for schema inference and no-op tests."""
    return pd.DataFrame(
        {
            "name": ["Alice", "Bob", "Carol"],
            "age": [25, 30, 35],
            "city": ["NYC", "LA", "NYC"],
            "signup": ["2024-01-01", "2024-02-01", "2024-03-01"],
            "price": [3.5, 4.0, 5.25],
        }
    )
