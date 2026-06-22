package com.dralit.DeskEye

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
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
 *   3) Corregir la rotación del sensor (la previsualización de CameraX la
 *      corrige automáticamente, pero los bytes "crudos" del analyzer no).
 */
object ImageUtils {

    /**
     * Convierte un frame de la cámara a un array de bytes JPEG, ya orientado
     * correctamente según [androidx.camera.core.ImageInfo.getRotationDegrees].
     *
     * @param quality calidad de compresión JPEG (0-100). Valores entre 50-75
     *                ofrecen un buen compromiso entre tamaño y fluidez para streaming.
     */
    fun imageProxyToJpeg(image: ImageProxy, quality: Int = 70): ByteArray {
        val nv21 = yuv420888ToNv21(image)
        val yuvImage = YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)

        val rawJpegStream = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, image.width, image.height), quality, rawJpegStream)
        val rawJpeg = rawJpegStream.toByteArray()

        val rotationDegrees = image.imageInfo.rotationDegrees
        return if (rotationDegrees != 0) {
            rotateJpeg(rawJpeg, rotationDegrees, quality)
        } else {
            rawJpeg
        }
    }

    /**
     * Empaqueta los planos Y, U, V de un [ImageProxy] en formato YUV_420_888
     * en un único array NV21 (Y seguido de V/U intercalados), respetando
     * rowStride/pixelStride de cada plano (no siempre coinciden con el ancho/alto).
     */
    private fun yuv420888ToNv21(image: ImageProxy): ByteArray {
        val width = image.width
        val height = image.height

        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val ySize = width * height
        val chromaSize = width * height / 2
        val nv21 = ByteArray(ySize + chromaSize)

        // --- Plano Y ---
        var pos = 0
        val yBuffer = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPixelStride = yPlane.pixelStride

        if (yPixelStride == 1 && yRowStride == width) {
            // Caso rápido: el buffer ya es contiguo y compacto.
            yBuffer.get(nv21, 0, ySize)
            pos = ySize
        } else {
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

        for (row in 0 until chromaHeight) {
            for (col in 0 until chromaWidth) {
                val vIndex = row * vRowStride + col * vPixelStride
                val uIndex = row * uRowStride + col * uPixelStride
                nv21[pos++] = vBuffer.get(vIndex)
                nv21[pos++] = uBuffer.get(uIndex)
            }
        }

        return nv21
    }

    private fun rotateJpeg(jpegBytes: ByteArray, rotationDegrees: Int, quality: Int): ByteArray {
        val bitmap = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
            ?: return jpegBytes

        val matrix = Matrix().apply { postRotate(rotationDegrees.toFloat()) }
        val rotatedBitmap = Bitmap.createBitmap(
            bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true
        )

        val out = ByteArrayOutputStream()
        rotatedBitmap.compress(Bitmap.CompressFormat.JPEG, quality, out)

        bitmap.recycle()
        rotatedBitmap.recycle()

        return out.toByteArray()
    }
}
