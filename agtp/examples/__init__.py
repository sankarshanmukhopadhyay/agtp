"""
Optional example modules. Nothing here is loaded by default. Servers
opt into custom methods by importing the relevant submodule, e.g.

    from agtp.examples import custom_methods  # noqa: F401

before starting the server. The agtp.server module also accepts
`--load-module agtp.examples.custom_methods` for the same effect.
"""
