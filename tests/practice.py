import asyncio

async def task(name,time):
    print(f"开始执行{name}")
    await asyncio.sleep(time)
    print(f"{name}执行结束")
    return name
async def say_hello():
    tasks=[task("A",2),task("B",1),task("C",1.5)]
    for coro in asyncio.as_completed(tasks):
        result=await coro
        print(f"完成{result}")
if __name__=="__main__":
 s=say_hello()
 asyncio.run(s)
