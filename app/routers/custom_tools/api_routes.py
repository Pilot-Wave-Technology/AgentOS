"""JSON API for custom tools — /api/custom-tools/."""
from fastapi import APIRouter

from app.routers.custom_tools.api_credentials import router as credentials_router
from app.routers.custom_tools.api_mcp import router as mcp_router
from app.routers.custom_tools.api_openapi import router as openapi_router
import app.routers.custom_tools.api_tools as _tools

router = APIRouter(prefix="/api/custom-tools")

# Fixed-prefix sub-routers first to prevent /{tool_name} shadowing their paths.
router.include_router(credentials_router)
router.include_router(openapi_router)
router.include_router(mcp_router)

# Tool CRUD registered directly — FastAPI raises "Prefix and path cannot be both
# empty" when include_router is called with a sub-router whose prefix="" and routes
# at path="". Credentials/OpenAPI work because they carry non-empty prefixes.
router.get("")(_tools.api_list_tools)
router.post("", status_code=201)(_tools.api_create_tool)
router.post("/bulk/credential")(_tools.api_bulk_set_credential)
router.get("/{tool_name}")(_tools.api_get_tool)
router.patch("/{tool_name}")(_tools.api_update_tool)
router.post("/{tool_name}/disable")(_tools.api_disable_tool)
router.post("/{tool_name}/enable")(_tools.api_enable_tool)
router.post("/{tool_name}/test")(_tools.api_test_tool)
router.post("/{tool_name}/regenerate")(_tools.api_regenerate_tool)
