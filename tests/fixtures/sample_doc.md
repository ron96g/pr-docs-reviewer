# HTTP Client

The HTTP client library provides a simple interface for making HTTP requests.

## Installation

```bash
pip install our-http-client
```

## Configuration

The Client accepts `timeout` and `base_url` parameters.

- `base_url` (str): The base URL for all requests.
- `timeout` (int, default=30): Request timeout in seconds.

## Usage

```python
from our_client import Client

client = Client("https://api.example.com")
response = client.request("GET", "/users")
```

## Connection Pool

For high-throughput applications, use the `ConnectionPool`:

```python
from our_client import ConnectionPool

pool = ConnectionPool(max_connections=20)
```

### Pool Configuration

The pool accepts `max_connections` and `timeout` parameters.

## Error Handling

The client raises `ConnectionError` on network failures and `TimeoutError`
when requests exceed the configured timeout.

## Changelog

### v2.0.0

- Rewrote connection handling
- Added connection pool support
