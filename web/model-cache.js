const DATABASE = 'mia.motion-studio.models';
const STORE = 'models';
let connection;

function openDatabase() {
  if (connection) return connection;
  connection = new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error('浏览器不支持 IndexedDB，仍可上传并生成，但不能保存在本机。'));
      return;
    }
    const request = indexedDB.open(DATABASE, 1);
    let failed = false;
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: 'id' });
    request.onerror = () => { failed = true; reject(request.error); };
    request.onblocked = () => { failed = true; reject(new Error('缓存数据库被其他页面占用，请关闭其他工作台后刷新。')); };
    request.onsuccess = () => {
      const db = request.result;
      if (failed) { db.close(); return; }
      db.onversionchange = () => { db.close(); connection = null; };
      resolve(db);
    };
  });
  connection.catch(() => { connection = null; });
  return connection;
}

async function transact(mode, operation) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE, mode);
    const request = operation(transaction.objectStore(STORE));
    transaction.oncomplete = () => resolve(request.result);
    transaction.onerror = () => reject(transaction.error || request.error || new Error('无法访问本机模型缓存。'));
    transaction.onabort = () => reject(transaction.error || new Error('模型缓存操作被中断。'));
  });
}

export async function saveModel(file) {
  // getRandomValues works on HTTP origins too; randomUUID/subtle require HTTPS.
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
  const record = { id, name: file.name, size: file.size, lastModified: file.lastModified, savedAt: Date.now(), blob: file };
  await transact('readwrite', store => store.put(record));
  return record;
}

export async function listModels() {
  const records = await transact('readonly', store => store.getAll());
  return records.sort((a, b) => b.savedAt - a.savedAt || a.id.localeCompare(b.id));
}

export function removeModel(id) {
  return transact('readwrite', store => store.delete(id));
}
