package com.dralit.DeskEye

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream

/**
 * Utilidades de conversión de imagen.
 *
 * CameraX entrega los frames de [androidx.camera.core.ImageAnalysis] en formato
 * YUV_420_888. Para poder servirlos como MJPEG necesitamos:
 *   1) Empaquetar los 3 planos (Y, U, V) en un buffer NV21 contiguo.
 *   2) Comprimir ese NV21 a JPEG con [YuvImage].
 *   3) Calcular la rotación necesaria (la previsualización de CameraX la
 *      corrige automáticamente, pero los bytes "crudos" del analyzer no).
 *
 * La rotación ya NO se aplica aquí. Antes cada frame se comprimía a JPEG, se
 * decodificaba a Bitmap, se rotaba y se volvía a comprimir (doble codificación:
 * más CPU, más basura para el GC, más calor y peor calidad). Ahora el JPEG sale
 * una sola vez y la rotación viaja como metadato ([JpegFrame.rotationDegrees]);
 * el cliente la aplica.
 */
object ImageUtils {

    // Buffers reutilizados entre frames. El analyzer corre en un único hilo, y
    // imageProxyToJpeg es @Synchronized por si alguna vez se usa desde otro.
    // Evita ~0,5 MB de basura por frame (a 30 fps, ~15 MB/s) y los tirones del GC.
    private var nv21Buffer = ByteArray(0)
    private var uRowBuffer = ByteArray(0)
    private var vRowBuffer = ByteArray(0)
    private val jpegStream = ByteArrayOutputStream(64 * 1024)

    /**
     * Convierte un frame de la cámara a JPEG (sin rotar) y devuelve, junto a los
     * bytes, los grados en sentido horario que el receptor debe girarlo para
     * verlo derecho ([androidx.camera.core.ImageInfo.getRotationDegrees] más el
     * giro manual pedido por el usuario).
     *
     * @param quality calidad de compresión JPEG (0-100). Valores entre 50-75
     *                ofrecen un buen compromiso entre tamaño y fluidez para streaming.
     */
    @Synchronized
    fun imageProxyToJpeg(
        image: ImageProxy,
        quality: Int = 70,
        additionalRotation: Int = 0
    ): JpegFrame {
        val nv21 = yuv420888ToNv21(image)
        val yuvImage = YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)

        jpegStream.reset()
        yuvImage.compressToJpeg(Rect(0, 0, image.width, image.height), quality, jpegStream)

        val totalRotation = (image.imageInfo.rotationDegrees + additionalRotation).mod(360)
        return JpegFrame(jpegStream.toByteArray(), totalRotation)
    }

    /**
     * Empaqueta los planos Y, U, V de un [ImageProxy] en formato YUV_420_888
     * en un único array NV21 (Y seguido de V/U intercalados), respetando
     * rowStride/pixelStride de cada plano (no siempre coinciden con el ancho/alto).
     *
     * Rendimiento: se copian filas enteras con `ByteBuffer.get(dst, off, len)` y solo
     * el intercalado V/U se hace sobre arrays (rápido). Antes se llamaba a
     * `ByteBuffer.get(index)` una vez por cada byte de croma (~150 000 llamadas por
     * frame). Devuelve un buffer reutilizado: solo es válido hasta la siguiente llamada.
     * (Se asumen dimensiones pares, como 640x480.)
     */
    private fun yuv420888ToNv21(image: ImageProxy): ByteArray {
        val width = image.width
        val height = image.height

        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val ySize = width * height
        val totalSize = ySize + width * height / 2
        if (nv21Buffer.size != totalSize) nv21Buffer = ByteArray(totalSize)
        val nv21 = nv21Buffer

        // --- Plano Y ---
        val yBuffer = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPixelStride = yPlane.pixelStride

        if (yPixelStride == 1) {
            if (yRowStride == width) {
                // Caso rápido: el buffer ya es contiguo y compacto.
                yBuffer.position(0)
                yBuffer.get(nv21, 0, ySize)
            } else {
                // Filas con padding: se copia el ancho útil de cada fila.
                for (row in 0 until height) {
                    yBuffer.position(row * yRowStride)
                    yBuffer.get(nv21, row * width, width)
                }
            }
        } else {
            var pos = 0
            for (row in 0 until height) {
                for (col in 0 until width) {
                    nv21[pos++] = yBuffer.get(row * yRowStride + col * yPixelStride)
                }
            }
        }

        // --- Planos U/V intercalados como VU (formato NV21) ---
        val uBuffer = uPlane.buffer
        val vBuffer = vPlane.buffer
        val uRowStride = uPlane.rowStride
        val uPixelStride = uPlane.pixelStride
        val vRowStride = vPlane.rowStride
        val vPixelStride = vPlane.pixelStride

        val chromaHeight = height / 2
        val chromaWidth = width / 2

        // Bytes que ocupa una fila de croma en cada plano. El último elemento no
        // necesita el padding hasta el siguiente píxel (en la última fila puede faltar).
        val uRowLen = (chromaWidth - 1) * uPixelStride + 1
        val vRowLen = (chromaWidth - 1) * vPixelStride + 1
        if (uRowBuffer.size < uRowLen) uRowBuffer = ByteArray(uRowLen)
        if (vRowBuffer.size < vRowLen) vRowBuffer = ByteArray(vRowLen)
        val uRow = uRowBuffer
        val vRow = vRowBuffer

        var pos = ySize
        for (row in 0 until chromaHeight) {
            vBuffer.position(row * vRowStride)
            vBuffer.get(vRow, 0, vRowLen)
            uBuffer.position(row * uRowStride)
            uBuffer.get(uRow, 0, uRowLen)

            var vi = 0
            var ui = 0
            for (col in 0 until chromaWidth) {
                nv21[pos++] = vRow[vi]
                nv21[pos++] = uRow[ui]
                vi += vPixelStride
                ui += uPixelStride
            }
        }

        return nv21
    }

    /**
     * Decodifica el JPEG de un [JpegFrame] a Bitmap SIN rotar (solo para el preview de la
     * pantalla; el giro se aplica al dibujar, con una matriz, sin crear otro bitmap).
     *
     * Si se pasa [reuse], el JPEG se decodifica dentro de ese mismo bitmap en lugar de
     * reservar uno nuevo (1,2 MB a 640x480): a 30 fps evita ~37 MB/s de basura nativa y
     * los tirones del GC. Si [reuse] no encaja (p. ej. cambió la resolución al cambiar de
     * cámara) se decodifica en un bitmap nuevo.
     */
    fun decodeJpeg(frame: JpegFrame, reuse: Bitmap? = null): Bitmap? {
        val options = BitmapFactory.Options().apply {
            inMutable = true
            inPreferredConfig = Bitmap.Config.ARGB_8888
            if (reuse != null && !reuse.isRecycled) inBitmap = reuse
        }
        return try {
            BitmapFactory.decodeByteArray(frame.jpeg, 0, frame.jpeg.size, options)
        } catch (e: IllegalArgumentException) {
            BitmapFactory.decodeByteArray(
                frame.jpeg, 0, frame.jpeg.size,
                BitmapFactory.Options().apply { inMutable = true }
            )
        }
    }
}
