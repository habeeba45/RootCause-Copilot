/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'Segoe UI', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Cascadia Code', 'Consolas', 'monospace'],
      },
      colors: {
        ink: { 950: '#070b14', 900: '#0b1120', 850: '#0f172a', 800: '#131c31', 700: '#1c2740', 600: '#2a3756' },
      },
      keyframes: {
        dash: { to: { strokeDashoffset: '-20' } },
        fadein: { from: { opacity: 0, transform: 'translateY(4px)' }, to: { opacity: 1, transform: 'none' } },
      },
      animation: { dash: 'dash 0.8s linear infinite', fadein: 'fadein .35s ease-out' },
    },
  },
  plugins: [],
}
