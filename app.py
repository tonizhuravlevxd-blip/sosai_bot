# backend/app.py
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import asyncio
import os
import logging

# ===== Настройка логов =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sosai_web")

app = FastAPI()
queue = asyncio.Queue()

logger.info("FastAPI app created")

# ===== Модель запроса =====
class Prompt(BaseModel):
    prompt: str
    mode: str

# ===== Главная страница =====
html_content = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sosai AI</title>
<style>
body { margin:0; font-family:Arial; background:#0f0f0f; color:white; }
.header { padding:20px; text-align:center; font-size:24px; background:#1a1a1a; }
.container { padding:20px; }
button { width:100%; padding:15px; margin:5px 0; border:none; border-radius:10px; background:#00ffcc; font-size:16px; cursor:pointer; }
input { width:100%; padding:15px; border-radius:10px; border:none; margin-bottom:10px; }
.card { background:#1a1a1a; padding:15px; border-radius:15px; margin-top:10px; }
</style>
</head>
<body>
<div class="header">🚀 Sosai AI</div>
<div class="container">
<div class="card">
<h3>🎨 Генерация</h3>
<button onclick="setMode('image')">Изображение</button>
<button onclick="setMode('video')">Видео</button>
<button onclick="setMode('music')">Музыка</button>
</div>
<div class="card">
<h3>✏ Ввод</h3>
<input id="prompt" placeholder="Введите запрос...">
<button onclick="generate()">Создать</button>
</div>
<div class="card">
<h3>👤 Аккаунт</h3>
<p>Free: 5 генераций</p>
<p>Premium: 200 генераций</p>
<button onclick="buy()">Купить Premium</button>
</div>
<div class="card">
<h3>📊 Статус</h3>
<p id="status">Ожидание...</p>
</div>
<div class="card">
<h3>💡 Результат</h3>
<div id="result"></div>
</div>
</div>
<script>
let mode='image';
function setMode(m){ mode=m; document.getElementById('status').innerText='Выбран режим: '+m; }
async function generate(){
    let prompt=document.getElementById('prompt').value;
    document.getElementById('status').innerText='⏳ Генерация...';
    let res=await fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:prompt,mode:mode})});
    let data=await res.json();
    document.getElementById('status').innerText='✅ Готово';
    if(data.type==='image'){
        document.getElementById('result').innerHTML='<img src="'+data.url+'" style="max-width:100%; border-radius:10px;">';
    } else {
        document.getElementById('result').innerText=data.result;
    }
}
function buy(){ alert('💎 Premium: 500 руб / 30 дней'); }
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    logger.info("GET / -> index page")
    return html_content

# ===== Генерация =====
async def fal_generate(prompt: str, mode: str):
    logger.info(f"Generating: prompt='{prompt}' mode='{mode}'")
    await asyncio.sleep(1)  # имитация работы
    if mode=="image":
        url = f"https://via.placeholder.com/512x512.png?text={prompt.replace(' ','+')}"
        logger.info(f"Image URL generated: {url}")
        return {"type":"image","url":url}
    result_text = f"{mode.upper()} GENERATED: {prompt}"
    logger.info(f"Result generated: {result_text}")
    return {"type":"text","result":result_text}

@app.post("/generate")
async def generate(data: Prompt):
    logger.info(f"POST /generate with data: {data}")
    try:
        result = await fal_generate(data.prompt, data.mode)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Error in generate")
        return JSONResponse({"type":"text","result":"❌ Ошибка генерации"}, status_code=500)

# ===== Запуск =====
if __name__=="__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Uvicorn on port {port}")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
