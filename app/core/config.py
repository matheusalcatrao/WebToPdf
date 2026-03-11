import os


class Settings:
    app_name: str = "WebToPdf"
    port: int = int(os.environ.get("PORT", 5001))
    scroll_pause: float = 0.2
    download_workers: int = 8


settings = Settings()
