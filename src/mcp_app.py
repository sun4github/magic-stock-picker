"""The FastMCP server instance, and the logger every tool writes to.

Its own module for one reason: `data/` and `persistence/` both register tools with
`@mcp.tool()`, and both log. If either owned the instance the other would have to
import it, and importing a package of tools to get at the server object is how an
import cycle starts.

The logger keeps the name "mcp_server" it has always had. It appears in every log
line the tools emit, and renaming it would silently break any log filter or grep
that predates this split.
"""
import logging

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("MagicFormulaSkepticalDecomposer")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_server")
