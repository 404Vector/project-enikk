"""Platform layer.

- ``domain``   immutable value objects describing what we deploy and where
- ``ports``    Protocols for external systems (what use cases depend on)
- ``adapters`` concrete implementations (imported only by composition roots)

This ``__init__`` deliberately does NOT import ``adapters`` so that importing the
package never drags infrastructure code into the application layer.
"""
