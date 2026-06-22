package com.dralit.DeskEye

import android.app.Application
import android.util.Log
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.lifecycle.AndroidViewModel
import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.IOException
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections
import java.util.concurrent.atomic.AtomicLong

/** Estado de UI expuesto por el ViewModel a la pantalla Compose. */
data class CameraUiState(
    val isServerRunning: Boolean = false,
    val ipAddress: String = "—",
    val port: Int = 4444,
    val framesServed: Long = 0,
    val errorMessage: String? = null
)

class CameraViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val TAG = "CameraViewModel"

        /** Límite de FPS hacia el repositorio (no afecta a la previsualización en pantalla). */
        private const val TARGET_FPS = 15
        private const val JPEG_QUALITY = 70
    }

    private val frameRepository = FrameRepository()
    private var mjpegServer: MjpegHttpServer? = null

    private val _uiState = MutableStateFlow(CameraUiState())
    val uiState: StateFlow<CameraUiState> = _uiState.asStateFlow()

    private val frameCounter = AtomicLong(0)
    private var lastAnalyzedTimestampMs = 0L
    private val minFrameIntervalMs = 1000L / TARGET_FPS


    val imageAnalyzer = ImageAnalysis.Analyzer { imageProxy -> processFrame(imageProxy) }

    private fun processFrame(imageProxy: ImageProxy) {
        val now = System.currentTimeMillis()
        if (now - lastAnalyzedTimestampMs < minFrameIntervalMs) {
            imageProxy.close()
            return
        }
        lastAnalyzedTimestampMs = now

        try {
            val jpeg = ImageUtils.imageProxyToJpeg(imageProxy, quality = JPEG_QUALITY)
            frameRepository.updateFrame(jpeg)

            val count = frameCounter.incrementAndGet()
            if (count % 30 == 0L) { // refrescamos el contador en UI cada ~2s a 15fps
                _uiState.value = _uiState.value.copy(framesServed = count)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error processing frame", e)
        } finally {
            imageProxy.close()
        }
    }

    /** Arranca el servidor MJPEG en el puerto indicado. No hace nada si ya está activo. */
    fun startServer(port: Int = 8080) {
        if (mjpegServer != null) return

        try {
            val server = MjpegHttpServer(port, frameRepository)
            server.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
            mjpegServer = server

            _uiState.value = _uiState.value.copy(
                isServerRunning = true,
                ipAddress = getLocalIpAddress(),
                port = port,
                framesServed = 0,
                errorMessage = null
            )
        } catch (e: IOException) {
            Log.e(TAG, "Unable to start the server", e)
            _uiState.value = _uiState.value.copy(
                isServerRunning = false,
                errorMessage = "Unable to start the server on port $port: ${e.message}"
            )
        }
    }

    /** Detiene el servidor MJPEG si está en marcha. */
    fun stopServer() {
        mjpegServer?.stop()
        mjpegServer = null
        _uiState.value = _uiState.value.copy(isServerRunning = false)
    }

    /**
     * Busca una dirección IPv4 no-loopback en las interfaces de red disponibles,
     * priorizando la interfaz WiFi (wlan0) ya que es el caso de uso principal
     * (PC y móvil en la misma red local).
     */
    private fun getLocalIpAddress(): String {
        return try {
            val interfaces = Collections.list(NetworkInterface.getNetworkInterfaces())

            val candidates = interfaces.flatMap { intf ->
                Collections.list(intf.inetAddresses)
                    .filterIsInstance<Inet4Address>()
                    .filter { !it.isLoopbackAddress }
                    .map { intf.name to it.hostAddress }
            }

            candidates.firstOrNull { (name, _) -> name.contains("wlan") }?.second
                ?: candidates.firstOrNull()?.second
                ?: "Not available"
        } catch (e: Exception) {
            Log.e(TAG, "Unable to get local IP address", e)
            "Not available"
        }
    }

    override fun onCleared() {
        super.onCleared()
        stopServer()
    }
}
