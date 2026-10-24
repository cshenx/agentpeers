"""Drive the same request steps with native blocking HTTP or cloud async HTTP."""


def run(steps, post):
    value, error = None, None
    while True:
        try:
            request = steps.throw(error) if error is not None else steps.send(value)
        except StopIteration as done:
            return done.value
        try:
            value, error = post(*request), None
        except Exception as exc:
            value, error = None, exc


async def run_async(steps, post):
    value, error = None, None
    while True:
        try:
            request = steps.throw(error) if error is not None else steps.send(value)
        except StopIteration as done:
            return done.value
        try:
            value, error = await post(*request), None
        except Exception as exc:
            value, error = None, exc
