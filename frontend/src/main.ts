import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './assets/index.css'

const app = createApp(App)

// Pinia 必须在 router 之前：路由守卫里会用到 store
app.use(createPinia())
app.use(router)

app.mount('#app')
