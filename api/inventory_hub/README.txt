
Backend patch: add "configs" API.
Files:
- inventory_hub/configs.py  (rename to: inventory_hub/configs.py inside your project)

Integration in main.py:
---------------------------------
from .configs import router as configs_router
app.include_router(configs_router)
---------------------------------

Environment:
- INVENTORY_DATA_ROOT must point to your data root (already used elsewhere). Defaults to ./inventory-data

Paths created on demand:
- console/config.json
- shops/{shop}/config.json
- suppliers/{supplier}/state/config.json
