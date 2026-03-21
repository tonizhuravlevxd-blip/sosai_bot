import asyncio

# ================= IMAGE =================
async def generate_image(prompt: str):
    prompt = clean_prompt(prompt)

    url = "https://queue.fal.run/fal-ai/nano-banana"

    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "prompt": prompt,
        "num_images": 1
    }

    async with aiohttp.ClientSession() as session:

        async with session.post(url, json=payload, headers=headers) as r:
            data = await r.json()

        request_id = data.get("request_id")

        status_url = f"{url}/requests/{request_id}/status"
        result_url = f"{url}/requests/{request_id}"

        for _ in range(120):
            async with session.get(status_url, headers=headers) as s:
                status = await s.json()

                if status.get("status") == "COMPLETED":
                    async with session.get(result_url, headers=headers) as r:
                        result = await r.json()
                        return result["images"][0]["url"]

            await asyncio.sleep(1)

    return "error"


# ================= VIDEO =================
async def generate_video(prompt: str):
    prompt = clean_prompt(prompt)
    return f"VIDEO GENERATED: {prompt}"


# ================= MUSIC =================
async def generate_music(prompt: str):
    prompt = clean_prompt(prompt)
    return f"MUSIC GENERATED: {prompt}"
