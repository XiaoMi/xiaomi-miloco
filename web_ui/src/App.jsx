/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import './App.css'
import { Routes, Route, Navigate } from 'react-router-dom'
import { ThemeProvider } from './contexts/ThemeContext'
import GlobalSocketProvider from './contexts/GlobalSocketProvider'
import { LogViewerModal } from './components'
import Setup from './pages/Setup'
import Login from './pages/Login'
import Home from './pages/Home'
import SmartCenter from './pages/SmartCenter'
import McpService from './pages/McpService'
import Instant from './pages/Instant'
import Error500 from './pages/Error/Error500';
import LogManage from './pages/LogManage'
import Setting from './pages/Setting'
import ExecutionManage from './pages/ExecutionManage'
import ModelManage from './pages/ModelManage'
import DeviceManage from './pages/DeviceManage'

function App() {
  return (
    <ThemeProvider>
      <GlobalSocketProvider>
        <Routes>
          <Route path="/" element={<Navigate to="/home/instant" replace />} />
          <Route path="/setup" element={<Setup />} />
          <Route path="/login" element={<Login />} />
          <Route path="/500" element={<Error500 />} />
          <Route path="/home" element={<Home />} >
            <Route index element={<Navigate to="instant" replace />} />
            <Route path="instant" element={<Instant />} />
            <Route path="deviceManage" element={<DeviceManage />} />
            <Route path="smartCenter" element={<SmartCenter />} />
            <Route path="executionManage" element={<ExecutionManage />} />
            <Route path="mcpService" element={<McpService />} />
            <Route path="modelManage" element={<ModelManage />} />
            <Route path="deviceManage" element={<DeviceManage />} />
            <Route path="logManage" element={<LogManage />} />
            <Route path="setting" element={<Setting />} />
          </Route>
        </Routes>
        <LogViewerModal />
      </GlobalSocketProvider>
    </ThemeProvider>
  )
}

export default App
