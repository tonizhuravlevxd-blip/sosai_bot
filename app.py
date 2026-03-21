from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import asyncio
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sosai_web")

app = FastAPI()
queue = asyncio.Queue()

logger.info("FastAPI app created")

html_content = """..."""  # оставляем твой HTML код без изменений

@app.get("/", response_class=HTMLResponse)
async def home():
    logger.info("GET / -> index page")
    return html_content

async def fal_generate(prompt: str, mode: str):
    logger.info(f"Generating: prompt='{prompt}' mode='{mode}'")
    await asyncio.sleep(1)
    if mode == "image":
        url = f"https://via.placeholder.com/512x512.png?text={prompt.replace(' ','+')}"
        logger.info(f"Generated image URL: {url}")
        return {"type":"image","url":url}
    result_text = f"{mode.upper()} GENERATED: {prompt}"
    logger.info(f"Generated text result: {result_text}")
    return {"type":"text","result":result_text}

@app.post("/generate")
async def generate_endpoint(data: dict):
    prompt = data.get("prompt", "")
    mode = data.get("mode", "image")
    try:
        result = await fal_generate(prompt, mode)
        logger.info(f"Returning result: {result}")
        return JSONResponse(result)
    except Exception:
        logger.exception("Error in generate")
        return JSONResponse({"type":"text","result":"❌ Ошибка генерации"}, status_code=500)

if __name__=="__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Uvicorn on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
