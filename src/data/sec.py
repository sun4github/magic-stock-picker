"""SEC EDGAR: the 10-K text extractor.

Extracted verbatim from mcp_server.py. Bodies, comments and docstrings are
unchanged: the docstrings are the tool descriptions an MCP client reads, so
rewording one is a change to the server's public contract, not a tidy-up. Verified
with `tools/snapshot_tool_surface.py`, which diffs all 33 tool registrations and
every exported signature across the move.
"""
import os
import time

from mcp_app import mcp


# ==========================================
# TOOL 2: SEC EDGAR Extractor
# ==========================================
try:
    from edgar import set_identity, Company
except ImportError:
    pass


@mcp.tool()
def fetch_sec_10k_data(ticker: str) -> str:
    """
    Extracts Item 1 segment data and >10% customer concentration notes from SEC 10-K for a ticker.
    """
    sec_user_agent = os.getenv("SEC_USER_AGENT", "Your Name admin@domain.com")
    set_identity(sec_user_agent)
    
    # Rate limit logic: delay 0.12 seconds
    time.sleep(0.12)
    
    try:
        company = Company(ticker)
        if not company:
            return f"Could not find SEC data for {ticker}"
            
        filings = company.get_filings(form="10-K")
        if not filings:
            return f"No 10-K filings found for {ticker}"
            
        latest_10k = filings[0].obj()

        # edgar-tools exposes Item 1 (Business) via item indexing / the
        # .business property, not a `.item_1` attribute.
        try:
            item_1 = str(latest_10k["Item 1"] or "")
        except Exception:
            item_1 = str(getattr(latest_10k, "business", "") or "")

        content = f"--- SEC 10-K Data for {ticker} ---\n\n"
        content += item_1[:30000] # Limiting to 30k chars

        # Scan Item 1 (Business) + Item 1A (Risk Factors) for >10% customer
        # concentration notes. The TenK object has no `.text` attribute.
        scan_text = item_1
        try:
            scan_text += "\n\n" + str(getattr(latest_10k, "risk_factors", "") or "")
        except Exception:
            pass
        concentration_notes = []
        for paragraph in scan_text.split('\n\n'):
            if 'customer' in paragraph.lower() and '10%' in paragraph:
                concentration_notes.append(paragraph.strip())
                
        if concentration_notes:
            content += "\n\n--- Customer Concentration Notes (>10%) ---\n"
            content += "\n\n".join(concentration_notes[:5]) # Top 5 mentions
        
        return content
    except Exception as e:
        return f"Error fetching SEC data for {ticker}: {str(e)}"
