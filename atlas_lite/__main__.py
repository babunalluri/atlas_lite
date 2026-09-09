from atlas_lite.config import load_settings
import uvicorn


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "atlas_lite.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
