from fastapi import FastAPI

app = FastAPI(title="Kliniczny Asystent PL API")

@app.get("/health")
def health():
    return {"status": "ok"}
