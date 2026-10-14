import React from 'react'
import ReactDOM from 'react-dom/client'
import './i18n'  // Initialize i18n before App
import './api/client' // Protect every API fetch, including legacy pages and uploads.
import './index.css'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
