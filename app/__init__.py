from app.api import app

if __name__ == "__main__":
    import uvicorn
    from services.jackett_client import settings
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
