import os, uuid, sqlite3
from datetime import datetime, date

# ---- 兼容 zoneinfo：Py3.9+ 原生；Py3.6/3.7 用 backports.zoneinfo；再不行回退 UTC ----
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    try:
        from backports.zoneinfo import ZoneInfo  # pip install backports.zoneinfo
    except Exception:
        ZoneInfo = None

from flask import Flask, request, redirect, url_for, render_template, session, flash, abort
from PIL import Image

# ---------------- 基础配置 ----------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# 显式指定模板/静态目录，避免部署路径变化带来的相对路径问题
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)

# 生产环境请用随机复杂值（也可改为读文件）
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'replace-me-with-a-random-secret')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB

UPLOAD_DIR_WEB    = os.path.join(app.static_folder, 'uploads', 'web')
UPLOAD_DIR_THUMBS = os.path.join(app.static_folder, 'uploads', 'thumbs')
for d in (UPLOAD_DIR_WEB, UPLOAD_DIR_THUMBS):
    os.makedirs(d, exist_ok=True)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '123456')  # ← 后台密码
ALLOWED_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
DB_PATH = os.path.join(BASE_DIR, 'gallery.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS photos (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   filename_web   TEXT NOT NULL,
                   filename_thumb TEXT NOT NULL,
                   title          TEXT NOT NULL,
                   date           TEXT NOT NULL,          -- YYYY-MM-DD（按天分组）
                   uploaded_at    TEXT NOT NULL           -- ISO 时间戳
               );'''
        )


init_db()

# ---------------- 工具函数 ----------------

def allowed_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTS


def _unique(ext: str) -> str:
    return f"{uuid.uuid4().hex}{ext}"


def _open_validate(stream) -> Image.Image:
    try:
        img = Image.open(stream)
        img.verify()  # 只校验
        stream.seek(0)
        return Image.open(stream).convert('RGB')  # 统一 RGB
    except Exception as e:
        raise ValueError('不是有效的图片文件或已损坏') from e


def _save(img: Image.Image, max_side: int, save_path: str, quality: int):
    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)
    new_w, new_h = int(w * scale), int(h * scale)
    if scale < 1.0:
        try:
            img = img.resize((new_w, new_h), Image.LANCZOS)
        except Exception:
            img = img.resize((new_w, new_h))
    img.save(save_path, format='JPEG', quality=quality, optimize=True)


def tokyo_today_str() -> str:
    try:
        tz = ZoneInfo('Asia/Tokyo') if ZoneInfo else None
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.utcnow()
    return now.date().isoformat()

# ---------------- 过滤器 ----------------

from datetime import datetime, date  # 文件顶部已有

@app.template_filter('cn_date')
def cn_date(date_str) -> str:
    # 兼容 Python 3.6：不用 date.fromisoformat
    if isinstance(date_str, date):
        d = date_str
    else:
        try:
            # 期望格式 YYYY-MM-DD（我们就是按这个写入数据库的）
            d = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        except Exception:
            # 兜底：无法解析就原样返回，避免 500
            return str(date_str)
    wmap = ['一', '二', '三', '四', '五', '六', '日']
    return f"{d.year}年{d.month}月{d.day}日（星期{wmap[d.weekday()]}）"

# ---------------- 健康检查 ----------------

@app.route('/healthz')
def healthz():
    return 'ok', 200

# ---------------- 前台主页 ----------------

@app.route('/')
def index():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM photos ORDER BY date DESC, id DESC').fetchall()

    groups, last_date = [], None
    for r in rows:
        if r['date'] != last_date:
            groups.append({'date': r['date'], 'items': []})
            last_date = r['date']
        groups[-1]['items'].append(r)

    return render_template('index.html', groups=groups)

# ---------------- 登录/登出 ----------------

# 兼容：/login → /admin/login （你前端可链接 /login，更好记）
@app.route('/login', methods=['GET', 'POST'])
def login_alias():
    if request.method == 'POST':
        # 允许直接在 /login 提交密码（表单也能用 url_for('login_alias')）
        if (request.form.get('password') or '') == ADMIN_PASSWORD:
            session['authed'] = True
            flash('登录成功', 'ok')
            return redirect('admin')
        flash('密码错误', 'err')
    return render_template('login.html')


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if (request.form.get('password') or '') == ADMIN_PASSWORD:
            session['authed'] = True
            flash('登录成功', 'ok')
            return redirect('admin')
        flash('密码错误', 'err')
    return render_template('login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('authed', None)
    flash('已退出登录', 'ok')
    return redirect('login')

# ---------------- 后台面板与 CRUD ----------------

@app.route('/admin', methods=['GET'])
def admin():
    if not session.get('authed'):
        return redirect('login')
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM photos ORDER BY id DESC').fetchall()
    return render_template('admin.html', rows=rows)


@app.route('/admin/upload', methods=['POST'])
def admin_upload():
    if not session.get('authed'):
        return redirect('login')

    file = request.files.get('file')
    title = (request.form.get('title') or '').strip() or '未命名'
    date_str = (request.form.get('date') or '').strip() or tokyo_today_str()

    if not file or file.filename == '':
        flash('请选择要上传的图片', 'err')
        return redirect('admin')

    if not allowed_file(file.filename):
        flash('不支持的图片格式（仅限：jpg/jpeg/png/gif/webp）', 'err')
        return redirect('admin')

    try:
        img = _open_validate(file.stream)
        web_name = _unique('.jpg')
        thumb_name = _unique('.jpg')
        web_path = os.path.join(UPLOAD_DIR_WEB, web_name)
        thumb_path = os.path.join(UPLOAD_DIR_THUMBS, thumb_name)

        _save(img, 2000, web_path, quality=85)  # 展示图
        _save(img, 700,  thumb_path, quality=80)  # 缩略图

        with get_db() as conn:
            conn.execute(
                'INSERT INTO photos (filename_web, filename_thumb, title, date, uploaded_at) VALUES (?, ?, ?, ?, ?)',
                (web_name, thumb_name, title, date_str, datetime.utcnow().isoformat(timespec='seconds') + 'Z')
            )
        flash('上传成功', 'ok')
    except Exception as e:
        flash(f'上传失败：{e}', 'err')

    return redirect('admin')


@app.route('/admin/update/<int:pid>', methods=['POST'])
def admin_update(pid):
    if not session.get('authed'):
        return redirect('login')
    title = (request.form.get('title') or '').strip() or '未命名'
    date_str = (request.form.get('date') or '').strip() or tokyo_today_str()
    with get_db() as conn:
        conn.execute('UPDATE photos SET title=?, date=? WHERE id=?', (title, date_str, pid))
    flash('已更新', 'ok')
    return redirect('admin')


@app.route('/admin/delete/<int:pid>', methods=['POST'])
def admin_delete(pid):
    if not session.get('authed'):
        return redirect('login')
    with get_db() as conn:
        row = conn.execute('SELECT filename_web, filename_thumb FROM photos WHERE id=?', (pid,)).fetchone()
        if row:
            for p in (os.path.join(UPLOAD_DIR_WEB, row['filename_web']),
                      os.path.join(UPLOAD_DIR_THUMBS, row['filename_thumb'])):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
            conn.execute('DELETE FROM photos WHERE id=?', (pid,))
    flash('已删除', 'ok')
    return redirect('admin')

# ---------------- 错误页（可选） ----------------

@app.errorhandler(404)
def not_found(e):
    # 线上可自定义你自己的 404 页面
    return render_template('404.html') if os.path.exists(os.path.join(app.template_folder, '404.html')) else ('Not Found', 404)

# ---------------- 开发模式运行（线上用 gunicorn） ----------------

if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5000'))
    debug = os.environ.get('DEBUG', '0') == '1'
    print(f'\n==> 前台:  http://{host}:{port}/')
    print(f'==> 登录:  http://{host}:{port}/login  或  /admin/login  (默认密码 {ADMIN_PASSWORD})\n')
    app.run(host=host, port=port, debug=debug)
