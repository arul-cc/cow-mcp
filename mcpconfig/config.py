# from mcp.server.fastmcp import FastMCP
import fastmcp
import os
from constants import constants

if not constants.ENABLE_CCOW_API_TOOLS:

    port = os.environ.get('CCOW_MCP_SERVER_PORT', "")
    portInInt = 0

    try:
        portInInt = int(port)
        print(f"Starting the server with streamable on port {portInInt}")
    except ValueError:
        print("Starting the server with stdio.")
        # print(f"Environment variable 'CCOW_MCP_SERVER_PORT' is not a valid integer: {port}")

    # Initialize and run the server

    if portInInt > 1:
        fastmcp.settings.host = "0.0.0.0"
        fastmcp.settings.port = portInInt

mcp = fastmcp.FastMCP("ComplianceCow")
