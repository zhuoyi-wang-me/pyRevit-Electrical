// Load Transfer - tooltip animation. Draws every frame with GDI+ (C# 5: no string interpolation).
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.IO;

public static class LoadTransferAnim
{
    public const int W = 720, H = 405, FPS = 30;
    public const double DURATION = 14.8;

    // ---- palette (dark slate + cyan accent)
    static readonly Color BG = Color.FromArgb(15, 23, 42);
    static readonly Color PANEL = Color.FromArgb(30, 41, 59);
    static readonly Color PANEL2 = Color.FromArgb(39, 52, 73);
    static readonly Color LINE = Color.FromArgb(51, 65, 85);
    static readonly Color TEXT = Color.FromArgb(226, 232, 240);
    static readonly Color MUTED = Color.FromArgb(148, 163, 184);
    static readonly Color CYAN = Color.FromArgb(34, 211, 238);
    static readonly Color GREEN = Color.FromArgb(52, 211, 153);
    static readonly Color AMBER = Color.FromArgb(251, 191, 36);
    static readonly Color WHITE = Color.FromArgb(255, 255, 255);

    static Image icon;
    static string F = "Segoe UI";

    // ================================================================ helpers
    static double Clamp01(double v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
    static double Seg(double t, double a, double b) { return Clamp01((t - a) / (b - a)); }
    static double Ease(double x) { x = Clamp01(x); return x < 0.5 ? 4 * x * x * x : 1 - Math.Pow(-2 * x + 2, 3) / 2; }
    static double EaseOut(double x) { x = Clamp01(x); return 1 - Math.Pow(1 - x, 3); }
    static double Back(double x) { x = Clamp01(x); double c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.Pow(x - 1, 3) + c1 * Math.Pow(x - 1, 2); }
    static float Lerp(double a, double b, double k) { return (float)(a + (b - a) * k); }
    static Color A(Color c, double alpha) { return Color.FromArgb((int)(255 * Clamp01(alpha) * c.A / 255.0), c.R, c.G, c.B); }

    static GraphicsPath Round(RectangleF r, float rad)
    {
        GraphicsPath p = new GraphicsPath();
        float d = rad * 2;
        if (d > r.Height) d = r.Height;
        if (d > r.Width) d = r.Width;
        p.AddArc(r.X, r.Y, d, d, 180, 90);
        p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        p.CloseFigure();
        return p;
    }

    static void Box(Graphics g, RectangleF r, float rad, Color fill, Color? border)
    {
        if (r.Width < 1 || r.Height < 1) return;          // GDI+ refuses empty arcs (progress bar at 0)
        if (rad < 0.5f) rad = 0.5f;
        using (GraphicsPath p = Round(r, rad))
        {
            using (SolidBrush b = new SolidBrush(fill)) g.FillPath(b, p);
            if (border.HasValue) using (Pen pen = new Pen(border.Value, 1f)) g.DrawPath(pen, p);
        }
    }

    static void Text(Graphics g, string s, float size, Color c, float x, float y, bool bold = false, StringAlignment align = StringAlignment.Near, float w = 0)
    {
        using (Font f = new Font(F, size, bold ? FontStyle.Bold : FontStyle.Regular, GraphicsUnit.Pixel))
        using (SolidBrush b = new SolidBrush(c))
        {
            if (w > 0)
            {
                StringFormat sf = new StringFormat();
                sf.Alignment = align; sf.LineAlignment = StringAlignment.Near; sf.Trimming = StringTrimming.EllipsisCharacter;
                sf.FormatFlags = StringFormatFlags.NoWrap;
                g.DrawString(s, f, b, new RectangleF(x, y, w, size * 1.6f), sf);
            }
            else g.DrawString(s, f, b, x, y);
        }
    }

    static float TextWidth(Graphics g, string s, float size, bool bold)
    {
        using (Font f = new Font(F, size, bold ? FontStyle.Bold : FontStyle.Regular, GraphicsUnit.Pixel))
            return g.MeasureString(s, f, PointF.Empty, StringFormat.GenericTypographic).Width;
    }

    static void Pill(Graphics g, string s, float x, float y, Color fg, double alpha, float size = 10.5f)
    {
        float w = TextWidth(g, s, size, true) + 14;
        Box(g, new RectangleF(x, y, w, size + 9), (size + 9) / 2, A(fg, 0.18 * alpha), A(fg, 0.55 * alpha));
        Text(g, s, size, A(fg, alpha), x + 7, y + 3.2f, true);
    }

    static void Check(Graphics g, float x, float y, double on, double alpha, double pop)
    {
        float s = 14 * (float)(1 + 0.25 * Math.Sin(Math.PI * Clamp01(pop)));
        float cx = x + 7, cy = y + 7;
        RectangleF r = new RectangleF(cx - s / 2, cy - s / 2, s, s);
        Box(g, r, 3.5f, on > 0.5 ? A(CYAN, alpha) : A(PANEL2, alpha), A(on > 0.5 ? CYAN : MUTED, alpha));
        if (on > 0.5)
        {
            using (Pen p = new Pen(A(BG, alpha), 2.2f))
            {
                p.StartCap = LineCap.Round; p.EndCap = LineCap.Round; p.LineJoin = LineJoin.Round;
                g.DrawLines(p, new PointF[] { new PointF(cx - 3.8f, cy + 0.2f), new PointF(cx - 1f, cy + 3f), new PointF(cx + 4f, cy - 3.2f) });
            }
        }
    }

    static void Cursor(Graphics g, float x, float y, double press)
    {
        float k = 1 - 0.12f * (float)press;
        PointF[] pts = {
            new PointF(0, 0), new PointF(0, 17), new PointF(4.2f, 13.2f), new PointF(7.2f, 19.8f),
            new PointF(10, 18.6f), new PointF(7.1f, 12.2f), new PointF(12.6f, 12.2f) };
        for (int i = 0; i < pts.Length; i++) pts[i] = new PointF(x + pts[i].X * k * 1.15f, y + pts[i].Y * k * 1.15f);
        using (GraphicsPath p = new GraphicsPath())
        {
            p.AddPolygon(pts);
            using (SolidBrush sh = new SolidBrush(Color.FromArgb(90, 0, 0, 0)))
            {
                g.TranslateTransform(1.5f, 2f); g.FillPath(sh, p); g.ResetTransform();
            }
            using (SolidBrush b = new SolidBrush(WHITE)) g.FillPath(b, p);
            using (Pen pen = new Pen(Color.FromArgb(20, 20, 20), 1.2f)) g.DrawPath(pen, p);
        }
    }

    static void Ripple(Graphics g, float x, float y, double k)
    {
        if (k <= 0 || k >= 1) return;
        float r = (float)(6 + 22 * EaseOut(k));
        using (Pen p = new Pen(A(CYAN, 0.9 * (1 - k)), 2.5f)) g.DrawEllipse(p, x - r, y - r, 2 * r, 2 * r);
    }

    // ================================================================ cursor track
    // (time, x, y) - the cursor glides between them; clicks at the listed times
    static readonly double[,] TRACK = {
        { 0.0, 470, 250 }, { 1.05, 236, 64 }, { 1.7, 236, 64 },
        { 2.9, 360, 300 }, { 3.5, 68, 188 }, { 4.35, 68, 188 },
        { 5.0, 560, 288 }, { 5.6, 560, 288 },
        { 8.9, 610, 210 }, { 11.2, 600, 290 }, { 11.9, 600, 290 }, { 13.2, 690, 395 } };
    static readonly double[] CLICKS = { 1.25, 3.85, 5.25, 11.45 };

    static PointF CursorAt(double t)
    {
        int n = TRACK.GetLength(0);
        if (t <= TRACK[0, 0]) return new PointF((float)TRACK[0, 1], (float)TRACK[0, 2]);
        for (int i = 0; i < n - 1; i++)
        {
            if (t <= TRACK[i + 1, 0])
            {
                double k = Ease((t - TRACK[i, 0]) / (TRACK[i + 1, 0] - TRACK[i, 0]));
                return new PointF(Lerp(TRACK[i, 1], TRACK[i + 1, 1], k), Lerp(TRACK[i, 2], TRACK[i + 1, 2], k));
            }
        }
        return new PointF((float)TRACK[n - 1, 1], (float)TRACK[n - 1, 2]);
    }

    static double Press(double t)
    {
        foreach (double c in CLICKS) { double d = Math.Abs(t - c); if (d < 0.12) return 1 - d / 0.12; }
        return 0;
    }

    // ================================================================ scenes
    static readonly RectangleF WIN = new RectangleF(28, 22, 664, 300);

    static void Ribbon(Graphics g, double t, double alpha)
    {
        if (alpha <= 0) return;
        // tab strip
        Box(g, new RectangleF(28, 22, 664, 26), 6, A(PANEL, alpha), null);
        string[] tabs = { "Architecture", "Systems", "Annotate", "Electrical", "MEP Tools" };
        float x = 44;
        for (int i = 0; i < tabs.Length; i++)
        {
            bool on = tabs[i] == "Electrical";
            float w = TextWidth(g, tabs[i], 12, on) + 18;
            if (on) Box(g, new RectangleF(x - 4, 25, w, 21), 5, A(CYAN, 0.16 * alpha), A(CYAN, 0.6 * alpha));
            Text(g, tabs[i], 12, A(on ? CYAN : MUTED, alpha), x + 5, 28, on);
            x += w + 6;
        }
        // panel with buttons
        Box(g, new RectangleF(28, 54, 664, 92), 8, A(PANEL, alpha), A(LINE, alpha));
        string[] btns = { "Auto Circuit", "Load Transfer", "Panel Checks", "Fixture Tools", "Equipment Power Check" };
        double hover = Seg(t, 0.85, 1.05) * (1 - Seg(t, 1.9, 2.2));
        for (int i = 0; i < btns.Length; i++)
        {
            float bx = 48 + i * 126, by = 62;
            bool target = i == 1;
            if (target && hover > 0) Box(g, new RectangleF(bx - 6, by - 2, 120, 78), 8, A(CYAN, 0.14 * hover * alpha), A(CYAN, 0.7 * hover * alpha));
            if (target && icon != null)
                g.DrawImage(icon, new RectangleF(bx + 38, by + 6, 32, 32));
            else
                Box(g, new RectangleF(bx + 38, by + 6, 32, 32), 7, A(PANEL2, alpha), A(LINE, alpha));
            Text(g, btns[i], 11, A(target ? TEXT : MUTED, alpha), bx - 6, by + 46, target, StringAlignment.Center, 120);
        }
        Text(g, "Electrical panel", 10, A(MUTED, alpha * 0.6), 40, 130, false);
        // the tooltip-like hint under the panel
        double tip = Seg(t, 0.3, 0.7) * (1 - Seg(t, 1.2, 1.5));
        if (tip > 0)
            Text(g, "Electrical  >  Electrical  >  Load Transfer", 12, A(MUTED, tip * alpha), 28, 176, false, StringAlignment.Center, 664);
    }

    static void WindowFrame(Graphics g, double appear, string subtitle)
    {
        float s = (float)(0.94 + 0.06 * Back(appear));
        RectangleF r = new RectangleF(WIN.X + WIN.Width * (1 - s) / 2, WIN.Y + WIN.Height * (1 - s) / 2 + (float)(14 * (1 - appear)), WIN.Width * s, WIN.Height * s);
        // shadow
        for (int i = 6; i >= 1; i--) Box(g, new RectangleF(r.X - i, r.Y - i + 4, r.Width + 2 * i, r.Height + 2 * i), 12 + i, Color.FromArgb((int)(10 * appear), 0, 0, 0), null);
        Box(g, r, 12, A(PANEL, appear), A(LINE, appear));
        Box(g, new RectangleF(r.X, r.Y, r.Width, 30), 12, A(PANEL2, appear), null);
        g.FillRectangle(new SolidBrush(A(PANEL2, appear)), r.X, r.Y + 18, r.Width, 12);
        if (icon != null && appear > 0.5) g.DrawImage(icon, new RectangleF(r.X + 10, r.Y + 6, 18, 18));
        Text(g, "Load Transfer", 13, A(TEXT, appear), r.X + 34, r.Y + 6, true);
        Text(g, subtitle, 11.5f, A(MUTED, appear), r.X + 140, r.Y + 7.5f);
        for (int i = 0; i < 3; i++)
            using (SolidBrush b = new SolidBrush(A(i == 2 ? Color.FromArgb(248, 113, 113) : LINE, appear)))
                g.FillEllipse(b, r.Right - 58 + i * 16, r.Y + 10, 10, 10);
    }

    static void SceneSources(Graphics g, double t, double alpha)
    {
        if (alpha <= 0) return;
        float x0 = 48, y0 = 66;
        Text(g, "2. SOURCE ELEMENTS", 10.5f, A(CYAN, alpha), x0, y0, true);
        Check(g, x0, y0 + 22, 1, alpha, 0); Text(g, "Electrical Equipment", 12.5f, A(TEXT, alpha), x0 + 22, y0 + 20);
        Check(g, x0 + 190, y0 + 22, 1, alpha, 0); Text(g, "Electrical Fixtures", 12.5f, A(TEXT, alpha), x0 + 212, y0 + 20);

        // source models box (the new feature)
        double glow = Seg(t, 2.7, 3.1) * (1 - Seg(t, 4.6, 5.0));
        RectangleF box = new RectangleF(x0, y0 + 50, 340, 116);
        Box(g, box, 9, A(BG, alpha), A(glow > 0 ? CYAN : LINE, alpha * (0.5 + 0.5 * glow)));
        Text(g, "Source models", 11, A(MUTED, alpha), x0 + 12, y0 + 58, true);
        Pill(g, "NEW", x0 + 112, y0 + 56, CYAN, alpha * Seg(t, 2.6, 2.9), 9.5f);
        double tick = Seg(t, 3.85, 3.95);
        Check(g, x0 + 14, y0 + 86, 1, alpha, 0);
        Text(g, "This model", 12.5f, A(TEXT, alpha), x0 + 36, y0 + 84);
        Check(g, x0 + 14, y0 + 116, tick, alpha, Seg(t, 3.85, 4.15));
        Text(g, "Linked:  KSS-MEP-ELE.rvt", 12.5f, A(TEXT, alpha), x0 + 36, y0 + 114);
        if (tick > 0) Pill(g, "read only", x0 + 222, y0 + 113, CYAN, alpha * Seg(t, 3.95, 4.3), 9.5f);
        Text(g, tick > 0 ? "2 of 2 models ticked" : "1 of 2 models ticked", 10.5f, A(tick > 0 ? GREEN : MUTED, alpha), x0 + 14, y0 + 142);

        // right column: target and matching
        float rx = 412;
        Text(g, "TARGET DETAIL ITEM", 10.5f, A(CYAN, alpha), rx, y0, true);
        Box(g, new RectangleF(rx, y0 + 18, 250, 28), 7, A(BG, alpha), A(LINE, alpha));
        Text(g, "SLD Panel : Distribution Board", 12, A(TEXT, alpha), rx + 10, y0 + 23);
        Text(g, "MATCH ON", 10.5f, A(CYAN, alpha), rx, y0 + 60, true);
        Box(g, new RectangleF(rx, y0 + 78, 250, 28), 7, A(BG, alpha), A(LINE, alpha));
        Text(g, "Panel Name   =   Panel Name", 12, A(TEXT, alpha), rx + 10, y0 + 83);
        // preview button
        double hot = Seg(t, 4.9, 5.1);
        double down = Press(t);
        RectangleF bt = new RectangleF(rx + 60, y0 + 198, 190, 34);
        Box(g, bt, 9, A(CYAN, alpha * (0.85 + 0.15 * hot - 0.25 * down)), null);
        Text(g, "PREVIEW MATCHES", 12.5f, A(BG, alpha), bt.X, bt.Y + 8, true, StringAlignment.Center, bt.Width);
    }

    static void SceneMatches(Graphics g, double t, double alpha)
    {
        if (alpha <= 0) return;
        float x0 = 48, y0 = 66;
        Text(g, "MATCH PREVIEW", 10.5f, A(CYAN, alpha), x0, y0, true);
        Text(g, "Target", 11, A(MUTED, alpha), x0 + 8, y0 + 24, true);
        Text(g, "Source", 11, A(MUTED, alpha), x0 + 120, y0 + 24, true);
        Text(g, "Status", 11, A(MUTED, alpha), x0 + 470, y0 + 24, true);
        g.DrawLine(new Pen(A(LINE, alpha), 1), x0, y0 + 44, x0 + 624, y0 + 44);
        string[,] rows = {
            { "DB-L1", "DB-L1  (Id 41213)", "MEP link", "Matched", "G" },
            { "AHU-01", "AHU-01  (Id 55217)", "MEP link", "Matched", "G" },
            { "DB-02", "AMBIGUOUS: 1 here, 1 in MEP link", "", "Not transferred", "A" } };
        for (int i = 0; i < 3; i++)
        {
            double k = EaseOut(Seg(t, 5.45 + i * 0.45, 5.95 + i * 0.45));
            if (k <= 0) continue;
            float y = y0 + 54 + i * 46 + (float)(10 * (1 - k));
            Color st = rows[i, 4] == "G" ? GREEN : AMBER;
            Box(g, new RectangleF(x0, y, 624, 38), 8, A(PANEL2, alpha * k), A(rows[i, 4] == "A" ? AMBER : LINE, alpha * k * (rows[i, 4] == "A" ? 0.6 : 1)));
            Text(g, rows[i, 0], 12.5f, A(TEXT, alpha * k), x0 + 10, y + 10, true);
            Text(g, rows[i, 1], 12.5f, A(rows[i, 4] == "A" ? AMBER : TEXT, alpha * k), x0 + 120, y + 10);
            if (rows[i, 2].Length > 0)
                Pill(g, rows[i, 2], x0 + 128 + TextWidth(g, rows[i, 1], 12.5f, false) + 10, y + 9, CYAN, alpha * k * Seg(t, 6.6, 7.0), 10);
            Pill(g, rows[i, 3], x0 + 470, y + 9, st, alpha * k, 10.5f);
        }
        double note = Seg(t, 7.3, 7.7);
        if (note > 0)
            Text(g, "Ambiguous rows are never written - fix the duplicate tag, or untick the model.", 11.5f, A(AMBER, alpha * note), x0, y0 + 200);
    }

    static void SceneTransfer(Graphics g, double t, double alpha)
    {
        if (alpha <= 0) return;
        float y0 = 66;
        Text(g, "OLD  vs  NEW", 10.5f, A(CYAN, alpha), 48, y0, true);
        // source card (linked panel)
        RectangleF a = new RectangleF(52, y0 + 30, 220, 110);
        Box(g, a, 10, A(BG, alpha), A(CYAN, alpha * 0.7));
        Text(g, "DB-L1", 15, A(TEXT, alpha), a.X + 14, a.Y + 10, true);
        Pill(g, "MEP link", a.X + 74, a.Y + 12, CYAN, alpha, 10);
        Text(g, "Elec Apparent Power", 11, A(MUTED, alpha), a.X + 14, a.Y + 44);
        Text(g, "25 000 VA", 21, A(TEXT, alpha), a.X + 14, a.Y + 62, true);
        // target card (detail item)
        RectangleF b = new RectangleF(448, y0 + 30, 220, 110);
        double done = Seg(t, 10.25, 11.0);
        Box(g, b, 10, A(BG, alpha), A(done > 0 ? GREEN : LINE, alpha * (0.6 + 0.4 * done)));
        Text(g, "Detail item  DB-L1", 13, A(TEXT, alpha), b.X + 14, b.Y + 11, true);
        Text(g, "TCL  (kW)", 11, A(MUTED, alpha), b.X + 14, b.Y + 44);
        double val = 25.0 * EaseOut(done);
        Text(g, val.ToString("0.00"), 21, A(done > 0 ? GREEN : TEXT, alpha), b.X + 14, b.Y + 62, true);
        if (done > 0.95) Pill(g, "was 0.00", b.X + 112, b.Y + 70, MUTED, alpha * Seg(t, 11.0, 11.3), 9.5f);
        // the flow between them
        PointF p0 = new PointF(a.Right + 6, a.Y + 55), p3 = new PointF(b.X - 6, b.Y + 55);
        PointF p1 = new PointF(p0.X + 60, p0.Y - 46), p2 = new PointF(p3.X - 60, p3.Y - 46);
        using (Pen dash = new Pen(A(LINE, alpha), 2f))
        {
            dash.DashStyle = DashStyle.Dash;
            g.DrawBezier(dash, p0, p1, p2, p3);
        }
        Pill(g, "x 0.001   W to kW", 312, y0 + 112, MUTED, alpha * Seg(t, 9.0, 9.4), 10);
        double fk = Ease(Seg(t, 9.2, 10.25));
        if (fk > 0 && fk < 1)
        {
            PointF q = Bez(p0, p1, p2, p3, fk);
            for (int i = 4; i >= 1; i--)
                using (SolidBrush gb = new SolidBrush(A(CYAN, 0.12 * alpha)))
                    g.FillEllipse(gb, q.X - 6 - i * 3, q.Y - 6 - i * 3, 12 + i * 6, 12 + i * 6);
            using (SolidBrush db = new SolidBrush(A(CYAN, alpha))) g.FillEllipse(db, q.X - 6, q.Y - 6, 12, 12);
        }
        // the transfer button
        double down = Press(t);
        RectangleF bt = new RectangleF(510, y0 + 200, 150, 34);
        Box(g, bt, 9, A(GREEN, alpha * (0.9 - 0.25 * down)), null);
        Text(g, "TRANSFER", 12.5f, A(BG, alpha), bt.X, bt.Y + 8, true, StringAlignment.Center, bt.Width);
        Text(g, "1 ambiguous row skipped", 11, A(AMBER, alpha * (1 - Seg(t, 11.6, 11.8))), 52, y0 + 208);
    }

    static PointF Bez(PointF a, PointF b, PointF c, PointF d, double k)
    {
        double u = 1 - k;
        return new PointF(
            (float)(u * u * u * a.X + 3 * u * u * k * b.X + 3 * u * k * k * c.X + k * k * k * d.X),
            (float)(u * u * u * a.Y + 3 * u * u * k * b.Y + 3 * u * k * k * c.Y + k * k * k * d.Y));
    }

    static void Toast(Graphics g, double t)
    {
        double k = Back(Seg(t, 11.7, 12.15)) * (1 - Seg(t, 14.2, 14.7));
        if (k <= 0) return;
        float w = 420, h = 54;
        float s = (float)(0.85 + 0.15 * k);
        // under the two cards and left of the Transfer button: the new value stays visible
        RectangleF r = new RectangleF(290 - w * s / 2, 238 - h * s / 2, w * s, h * s);
        double a = Clamp01(k);
        Box(g, new RectangleF(r.X - 3, r.Y + 3, r.Width + 6, r.Height + 4), 16, Color.FromArgb((int)(70 * a), 0, 0, 0), null);
        Box(g, r, 14, A(Color.FromArgb(6, 78, 59), a), A(GREEN, a));
        using (SolidBrush b = new SolidBrush(A(GREEN, a))) g.FillEllipse(b, r.X + 14, r.Y + r.Height / 2 - 13, 26, 26);
        using (Pen p = new Pen(A(BG, a), 3f))
        {
            p.StartCap = LineCap.Round; p.EndCap = LineCap.Round;
            float cx = r.X + 27, cy = r.Y + r.Height / 2;
            g.DrawLines(p, new PointF[] { new PointF(cx - 6, cy), new PointF(cx - 1.5f, cy + 5), new PointF(cx + 6.5f, cy - 5) });
        }
        Text(g, "2 values written into this model", 14 * s, A(WHITE, a), r.X + 52, r.Y + 8 * s, true);
        Text(g, "1 ambiguous row skipped   -   one Undo puts it back", 11.5f * s, A(GREEN, a), r.X + 52, r.Y + 30 * s);
    }

    static readonly string[] CAP = {
        "Click  Load Transfer",
        "Tick this model and / or the linked MEP model",
        "Preview matches  -  ambiguous rows are never written",
        "Check old  ->  new value, then Transfer",
        "Done  -  linked loads are now on the single-line diagram" };
    static readonly double[] CAP_T = { 0.0, 2.3, 5.35, 8.75, 11.75, 99 };

    static void Caption(Graphics g, double t)
    {
        int i = 0;
        while (i < CAP.Length - 1 && t >= CAP_T[i + 1]) i++;
        double inK = EaseOut(Seg(t, CAP_T[i], CAP_T[i] + 0.35));
        double outK = 1 - Seg(t, 14.3, 14.75);
        double a = inK * outK;
        RectangleF bar = new RectangleF(28, 340, 664, 44);
        Box(g, bar, 12, A(PANEL, outK), A(LINE, outK));
        // progress
        float prog = (float)Clamp01(t / DURATION);
        Box(g, new RectangleF(bar.X + 12, bar.Bottom - 6, (bar.Width - 24) * prog, 3), 1.5f, A(CYAN, 0.8 * outK), null);
        // step badge
        string num = i < 4 ? (i + 1).ToString() : "OK";
        float yoff = (float)(8 * (1 - inK));
        using (SolidBrush b = new SolidBrush(A(i < 4 ? CYAN : GREEN, a))) g.FillEllipse(b, bar.X + 12, bar.Y + 8 + yoff, 26, 26);
        Text(g, num, i < 4 ? 13 : 10.5f, A(BG, a), bar.X + 12, bar.Y + (i < 4 ? 11.5f : 13.5f) + yoff, true, StringAlignment.Center, 26);
        Text(g, CAP[i], 15, A(TEXT, a), bar.X + 48, bar.Y + 10 + yoff, true);
    }

    // ================================================================ one frame
    public static void Frame(Graphics g, double t)
    {
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
        g.InterpolationMode = InterpolationMode.HighQualityBicubic;
        g.Clear(BG);
        // faint grid in the background
        using (Pen gp = new Pen(Color.FromArgb(10, 255, 255, 255), 1))
        {
            for (int x = 0; x < W; x += 24) g.DrawLine(gp, x, 0, x, H);
            for (int y = 0; y < H; y += 24) g.DrawLine(gp, 0, y, W, y);
        }
        double ribbon = 1 - Seg(t, 1.7, 2.1);
        Ribbon(g, t, ribbon * Seg(t, 0, 0.25) + (t < 0.25 ? 0 : 0));
        double win = Seg(t, 1.85, 2.35) * (1 - Seg(t, 14.2, 14.7));
        if (win > 0)
        {
            string sub = t < 5.35 ? "-  choose sources" : (t < 8.75 ? "-  match preview" : "-  review and transfer");
            WindowFrame(g, EaseOut(win), sub);
            double s1 = Seg(t, 2.2, 2.5) * (1 - Seg(t, 5.3, 5.5));
            double s2 = Seg(t, 5.4, 5.6) * (1 - Seg(t, 8.65, 8.85));
            double s3 = Seg(t, 8.8, 9.0);
            SceneSources(g, t, s1 * win);
            SceneMatches(g, t, s2 * win);
            SceneTransfer(g, t, s3 * win);
        }
        Toast(g, t);
        Caption(g, t);
        // cursor and click ripples
        foreach (double c in CLICKS) { PointF cp = CursorAt(c); Ripple(g, cp.X, cp.Y, Seg(t, c, c + 0.55)); }
        double cur = Seg(t, 0.1, 0.4) * (1 - Seg(t, 13.0, 13.3));
        if (cur > 0) { PointF p = CursorAt(t); Cursor(g, p.X, p.Y, Press(t)); }
    }

    public static int Render(string outDir, string iconPath)
    {
        if (File.Exists(iconPath)) icon = Image.FromFile(iconPath);
        Directory.CreateDirectory(outDir);
        int frames = (int)Math.Round(DURATION * FPS);
        using (Bitmap bmp = new Bitmap(W, H, PixelFormat.Format24bppRgb))
        using (Graphics g = Graphics.FromImage(bmp))
        {
            for (int i = 0; i < frames; i++)
            {
                Frame(g, i / (double)FPS);
                bmp.Save(Path.Combine(outDir, string.Format("f{0:D4}.png", i)), ImageFormat.Png);
            }
        }
        return frames;
    }
}
