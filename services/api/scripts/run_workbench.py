"""Run the local Context Layer workbench API."""

import uvicorn

from waypoint.workbench_api import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")
