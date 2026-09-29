package com.dralit.DeskEye

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream


object ImageUtils {


    fun imageProxyToJpeg(
        image: ImageProxy,
        quality: Int = 70,
        additionalRotation: Int = 0
    ): JpegFrame {
        val nv21 = yuv420888ToNv21(image)
        val yuvImage = YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)

        val jpegStream = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, image.width, image.height), quality, jpegStream)

        val totalRotation = (image.imageInfo.rotationDegrees + additionalRotation).mod(360)
        return JpegFrame(jpegStream.toByteArray(), totalRotation)
    }


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
}
