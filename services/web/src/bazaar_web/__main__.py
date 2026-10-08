import os

import uvicorn

uvicorn.run("bazaar_web.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
