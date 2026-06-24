package com.dralit.DeskEye

import android.app.Application
import android.content.Intent
import android.util.Log
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections

data class CameraUiState(
    val isServerRunning: Boolean = false,
    val ipAddress: String = "—",
    val port: Int = 4444,
    val framesServed: Long = 0,
    val errorMessage: String? = null
)

class CameraViewModel(application: Application) : AndroidViewModel(application) {

    private val _uiState = MutableStateFlow(CameraUiState())
    val uiState: StateFlow<CameraUiState> = _uiState.asStateFlow()

    // Este analyzer es SOLO para la previsualización local si se requiere, 
    // pero la lógica de stream ahora vive en el servicio.
    val imageAnalyzer = ImageAnalysis.Analyzer { it.close() }

    init {
        // Observamos el estado del servicio para actualizar la UI
        viewModelScope.launch {
            CameraService.isRunning.collect { running ->
                _uiState.value = _uiState.value.copy(
                    isServerRunning = running,
                    ipAddress = if (running) getLocalIpAddress() else "—"
                )
            }
        }
        viewModelScope.launch {
            CameraService.framesServed.collect { frames ->
                _uiState.value = _uiState.value.copy(framesServed = frames)
            }
        }
        viewModelScope.launch {
            CameraService.port.collect { port ->
                _uiState.value = _uiState.value.copy(port = port)
            }
        }
    }

    fun startServer(port: Int = 4444) {
        val intent = Intent(getApplication(), CameraService::class.java).apply {
            putExtra("port", port)
        }
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
            getApplication<Application>().startForegroundService(intent)
        } else {
            getApplication<Application>().startService(intent)
        }
    }

    fun stopServer() {
        val intent = Intent(getApplication(), CameraService::class.java)
        getApplication<Application>().stopService(intent)
    }

    fun toggleCamera() {
        val intent = Intent(getApplication(), CameraService::class.java).apply {
            action = CameraService.ACTION_TOGGLE_CAMERA
        }
        getApplication<Application>().startService(intent)
    }

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
            Log.e("CameraViewModel", "Unable to get local IP address", e)
            "Not available"
        }
    }
}
