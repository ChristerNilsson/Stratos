from fasthtml.common import Button, H1, Main, P, Title, fast_app, serve

app, rt = fast_app()
counter = 0


@rt("/")
def get():
    return (
        Title("Counter"),
        Main(
            H1("Hello, world!"),
            P(str(counter), id="counter"),
            Button(
                "Räkna upp",
                hx_post="/increment",
                hx_target="#counter",
                hx_swap="outerHTML",
            ),
        ),
    )


@rt("/increment")
def post():
    global counter
    counter += 1
    return P(str(counter), id="counter")


serve(host="127.0.0.1", port=8000, reload=False)

