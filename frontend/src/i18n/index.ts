// src/i18n/index.ts
// i18n setup with react-i18next

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import sk from './sk.json';
import en from './en.json';

// Get stored language or default to Slovak
const storedLang = typeof localStorage !== 'undefined' 
  ? localStorage.getItem('lang') || 'sk'
  : 'sk';

i18n
  .use(initReactI18next)
  .init({
    resources: {
      sk: { translation: sk },
      en: { translation: en },
    },
    lng: storedLang,
    fallbackLng: 'sk',
    interpolation: {
      escapeValue: false, // React already escapes
    },
    react: {
      useSuspense: false,
    },
  });

// Helper to change language and persist
export function setLanguage(lang: 'sk' | 'en'): void {
  i18n.changeLanguage(lang);
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem('lang', lang);
  }
}

// Get current language
export function getLanguage(): string {
  return i18n.language || 'sk';
}

export default i18n;
