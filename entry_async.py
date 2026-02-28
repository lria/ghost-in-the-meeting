# /app/entry_async.py
import os
import uvicorn

from main import app  # app originale (include già /transcription/)
from async_routes import router as async_router

app.include_router(async_router)

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)