import h11
import pytest


def test_chunked_request_accepts_crlf_terminator() -> None:
    connection = h11.Connection(h11.SERVER)
    connection.receive_data(
        b"POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    )

    assert isinstance(connection.next_event(), h11.Request)
    assert connection.next_event() == h11.Data(data=b"hello", chunk_start=True, chunk_end=True)
    assert isinstance(connection.next_event(), h11.EndOfMessage)


def test_chunked_request_rejects_malformed_terminator() -> None:
    connection = h11.Connection(h11.SERVER)
    connection.receive_data(
        b"POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhelloXX0\r\n\r\n"
    )

    assert isinstance(connection.next_event(), h11.Request)
    assert isinstance(connection.next_event(), h11.Data)
    with pytest.raises(h11.RemoteProtocolError, match="malformed chunk footer"):
        connection.next_event()
