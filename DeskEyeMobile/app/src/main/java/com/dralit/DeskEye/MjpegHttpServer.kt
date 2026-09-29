package com.dralit.DeskEye

import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.io.InputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock


class MjpegHttpServer(
    port: Int,
    private val frameRepository: FrameRepository,
    private val onToggle: () -> Unit,
    private val onRotateRight: () -> Unit = {},
    private val onRotateLeft: () -> Unit = {}
) : NanoHTTPD(port) {

    companion object {
        private const val BOUNDARY = "frameboundary"

        private const val SEND_BUFFER_BYTES = 128 * 1024
    }

    private val serverScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    init {

        setServerSocketFactory(object : NanoHTTPD.ServerSocketFactory {
            override fun create(): ServerSocket = object : ServerSocket() {
                override fun accept(): Socket {
                    val socket = super.accept()
                    socket.tcpNoDelay = true
                    socket.sendBufferSize = SEND_BUFFER_BYTES
                    return socket
                }
            }
        })
    }

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/stream" -> serveStream()
            "/rotation" -> serveRotation()
            "/toggle" -> serveToggle()
            "/rturn" -> serveRotateRight()
            "/lturn" -> serveRotateLeft()
            "/", "/index.html" -> serveIndexPage()
            else -> newFixedLengthResponse(
                Response.Status.NOT_FOUND, "text/plain", "404 Not Found"
            )
        }
    }

    private fun serveToggle(): Response {
        onToggle()
        return newFixedLengthResponse(Response.Status.OK, "text/plain", "Camera toggled")
    }

    private fun serveRotation(): Response {
        val rotation = frameRepository.frames.replayCache.firstOrNull()?.rotationDegrees ?: 0
        val response = newFixedLengthResponse(Response.Status.OK, "text/plain", rotation.toString())
        response.addHeader("Cache-Control", "no-store")
        return response
    }

    private fun serveRotateRight(): Response {
        onRotateRight()
        return newFixedLengthResponse(Response.Status.OK, "text/plain", "Camera rotated right 90°")
    }

    private fun serveRotateLeft(): Response {
        onRotateLeft()
        return newFixedLengthResponse(Response.Status.OK, "text/plain", "Camera rotated left 90°")
    }

    private fun serveStream(): Response {
        val mailbox = LatestChunkMailbox()

        val job = serverScope.launch {
            try {
                frameRepository.frames.collect { frame ->
                    mailbox.offer(buildMultipartChunk(frame))
                }
            } finally {
                mailbox.close()
            }
        }

        val stream = MailboxInputStream(mailbox) { job.cancel() }

        val response = newChunkedResponse(
            Response.Status.OK,
            "multipart/x-mixed-replace; boundary=$BOUNDARY",
            stream
        )
        response.addHeader("Cache-Control", "no-cache, private")
        response.addHeader("Pragma", "no-cache")
        response.addHeader("Connection", "close")
        return response
    }

    private fun buildMultipartChunk(frame: JpegFrame): ByteArray {
        val jpeg = frame.jpeg
        val header = (
            "--$BOUNDARY\r\n" +
            "Content-Type: image/jpeg\r\n" +
            "Content-Length: ${jpeg.size}\r\n" +
            "X-Rotation: ${frame.rotationDegrees}\r\n\r\n"
        ).toByteArray(Charsets.US_ASCII)
        val footer = "\r\n".toByteArray(Charsets.US_ASCII)

        val chunk = ByteArray(header.size + jpeg.size + footer.size)
        System.arraycopy(header, 0, chunk, 0, header.size)
        System.arraycopy(jpeg, 0, chunk, header.size, jpeg.size)
        System.arraycopy(footer, 0, chunk, header.size + jpeg.size, footer.size)
        return chunk
    }

    private fun serveIndexPage(): Response {
        val html = """
            <!DOCTYPE html>
            <html>
              <head>
                <title>DeskEye</title>
                <meta name="viewport" content="width=device-width, initial-scale=1" />
              </head>
              <body style="margin:0;background:#000;display:flex;align-items:center;justify-content:center;height:100vh;overflow:hidden;">
                <img id="v" src="/stream" style="max-width:100vw;max-height:100vh;" alt="stream" />
                <script>
                  var img = document.getElementById('v');
                  function syncRotation() {
                    fetch('/rotation', {cache: 'no-store'})
                      .then(function (r) { return r.text(); })
                      .then(function (t) {
                        var r = parseInt(t, 10) || 0;
                        var sideways = (r === 90 || r === 270);
                        img.style.maxWidth = sideways ? '100vh' : '100vw';
                        img.style.maxHeight = sideways ? '100vw' : '100vh';
                        img.style.transform = 'rotate(' + r + 'deg)';
                      })
                      .catch(function () {});
                  }
                  syncRotation();
                  setInterval(syncRotation, 500);
                </script>
              </body>
            </html>
        """.trimIndent()
        return newFixedLengthResponse(Response.Status.OK, "text/html", html)
    }

    override fun stop() {
        serverScope.cancel()
        super.stop()
    }


    private class LatestChunkMailbox {
        private val lock = ReentrantLock()
        private val notEmpty = lock.newCondition()
        private var pending: ByteArray? = null
        private var closed = false

        fun offer(chunk: ByteArray) {
            lock.withLock {
                if (closed) return@withLock
                pending = chunk            // sobrescribe el anterior si nadie lo ha leído
                notEmpty.signal()
            }
        }

        fun close() {
            lock.withLock {
                closed = true
                pending = null
                notEmpty.signalAll()
            }
        }

        fun take(): ByteArray? = lock.withLock {
            while (pending == null && !closed) {
                notEmpty.await()
            }
            if (closed) null else pending.also { pending = null }
        }
    }

    /** InputStream que va sirviendo a NanoHTTPD los chunks del [LatestChunkMailbox]. */
    private class MailboxInputStream(
        private val mailbox: LatestChunkMailbox,
        private val onClose: () -> Unit
    ) : InputStream() {

        private var current: ByteArray? = null
        private var pos = 0
        @Volatile private var closed = false

        override fun read(): Int {
            val single = ByteArray(1)
            val n = read(single, 0, 1)
            return if (n <= 0) -1 else (single[0].toInt() and 0xFF)
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (closed) return -1

            val active: ByteArray = current?.takeIf { pos < it.size } ?: run {
                val next = try {
                    mailbox.take()
                } catch (_: InterruptedException) {
                    null
                } ?: return -1
                current = next
                pos = 0
                next
            }

            val toCopy = minOf(active.size - pos, len)
            System.arraycopy(active, pos, b, off, toCopy)
            pos += toCopy
            return toCopy
        }

        override fun close() {
            if (closed) return
            closed = true
            mailbox.close()
            onClose()
        }
    }
}
