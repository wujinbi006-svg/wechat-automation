import asyncio, json, sys
import websockets

async def main():
    uri = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8010/ws/events"
    count = 0
    async with websockets.connect(uri) as ws:
        print(f"connected: {uri}")
        async for raw in ws:
            event = json.loads(raw)
            count += 1
            print(json.dumps(event, ensure_ascii=False))
            if count >= 1:
                break
    print(f"events={count}")

if __name__ == "__main__":
    asyncio.run(main())
