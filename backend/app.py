from fastapi import FastAPI
from pydantic import BaseModel
from core.py import fal_generate

app = FastAPI()

class Prompt(BaseModel):
    prompt: str
    mode: str

@app.get("/")
async def home():
    return {"status": "Sosai AI работает"}

@app.post("/generate")
async def generate(data: Prompt):

    if data.mode == "image":
        result = await fal_generate("banana2", data.prompt)
        return {"type": "image"}

    if data.mode == "music":
        return {"type": "music"}

    if data.mode == "video":
        return {"type": "video"}
