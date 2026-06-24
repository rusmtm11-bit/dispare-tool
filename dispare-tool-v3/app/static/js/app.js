// Dispare Trading — общие утилиты
// (основная логика в шаблонах страниц)

function formatPrice(price) {
    if (!price) return '—';
    return price.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + ' ₽';
}

function formatDate(isoStr) {
    if (!isoStr) return '';
    return new Date(isoStr).toLocaleDateString('ru-RU');
}
