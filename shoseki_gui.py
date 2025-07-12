import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import json
from shoseki_scraper import scrape_latest_weekly_and_estimate

class ShosekiGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Shoseki Rankings Scraper")
        self.geometry("400x250")
        self.create_widgets()

    def create_widgets(self):
        # Number of ranks
        ttk.Label(self, text="Number of ranks:").pack(pady=(10,0))
        self.limit_var = tk.IntVar(value=500)
        ttk.Entry(self, textvariable=self.limit_var, width=10).pack()

        # Weekly/Monthly selection
        self.period_var = tk.StringVar(value="weekly")
        frame = ttk.Frame(self)
        frame.pack(pady=10)
        ttk.Radiobutton(frame, text="Weekly", variable=self.period_var, value="weekly").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(frame, text="Monthly", variable=self.period_var, value="monthly").pack(side=tk.LEFT, padx=5)

        # Output file name
        ttk.Label(self, text="Output file name:").pack()
        self.file_var = tk.StringVar(value="shoseki_weekly_ranking.json")
        file_entry = ttk.Entry(self, textvariable=self.file_var, width=30)
        file_entry.pack()
        ttk.Button(self, text="Browse...", command=self.browse_file).pack(pady=5)

        # Progress bar
        self.progress = ttk.Progressbar(self, orient="horizontal", length=300, mode="determinate")
        self.progress.pack(pady=5)

        # Run button
        self.run_btn = ttk.Button(self, text="Run Scraper", command=self.run_scraper)
        self.run_btn.pack(pady=10)

        # Status
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, foreground="blue").pack(pady=5)

    def browse_file(self):
        filename = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if filename:
            self.file_var.set(filename)

    def run_scraper(self):
        self.run_btn.config(state=tk.DISABLED)
        self.status_var.set("Scraping... Please wait.")
        self.progress['value'] = 0
        threading.Thread(target=self._scrape_and_save, daemon=True).start()

    def _scrape_and_save(self):
        try:
            limit = self.limit_var.get()
            use_monthly = self.period_var.get() == "monthly"
            file_name = self.file_var.get()
            # Custom progress callback
            def progress_callback(current, total):
                self.progress['maximum'] = total
                self.progress['value'] = current
                self.update_idletasks()
            # Patch the scraper to use the callback
            from shoseki_scraper import _latest_article_url, _get_soup, _make_estimator, _extract_date_info, _parse_baseline, _extract_rank_list, _query_anilist_batch, _machine_translate
            post_url, category_type = _latest_article_url(use_monthly)
            soup = _get_soup(post_url)
            article_text = soup.get_text("\n", strip=True)
            estimator = _make_estimator(_parse_baseline(article_text))
            date_info = _extract_date_info(soup, is_monthly=use_monthly)
            rank_lines = _extract_rank_list(soup)
            total = min(limit, len(rank_lines))
            # Batch AniList queries
            unique_titles = list({title for _, title, _ in rank_lines})
            en_map = {}
            for i in range(0, len(unique_titles), 50):
                batch_result = _query_anilist_batch(unique_titles[i : i + 50])
                for jp, en in batch_result.items():
                    en_map[jp] = en
            results = []
            for idx, (rank, jp, volume) in enumerate(rank_lines):
                if rank > limit:
                    continue
                en_title = en_map.get(jp)
                source = "anilist" if en_title else "machine_translation"
                if not en_title:
                    en_title = _machine_translate(jp)
                results.append({
                    "rank": rank,
                    "jp_title": jp,
                    "en_title": en_title,
                    "en_source": source,
                    "volume": volume,
                    "estimated_sales": estimator(rank)
                })
                progress_callback(len(results), total)
            data = {
                "category_type": category_type,
                "date_info": date_info,
                "total_entries": len(results),
                "rankings": results
            }
            # try:
            #     # If using updated scraper, get date_info from it
            #     data = scrape_latest_weekly_and_estimate(limit=limit, use_monthly=use_monthly)
            # except Exception:
            #     pass
            with open(file_name, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            date_info = data.get("date_info", {})
            jp_date = date_info.get("jp_date", "(date not found)")
            en_date = date_info.get("en_date", jp_date)
            year = date_info.get("year", "?")
            month = date_info.get("month", "?")
            week = date_info.get("week", "?")
            self.status_var.set(f"Done! Saved to {file_name}\nJP Date: {jp_date}\nEN Date: {en_date}\nYear: {year}\nMonth: {month}\nWeek: {week}")
            messagebox.showinfo("Finished", f"Finished Processing\nJP Date: {jp_date}\nEN Date: {en_date}\nYear: {year}\nMonth: {month}\nWeek: {week}")
        except Exception as e:
            self.status_var.set("Error: " + str(e))
            messagebox.showerror("Error", str(e))
        finally:
            self.run_btn.config(state=tk.NORMAL)
            self.progress['value'] = 0

if __name__ == "__main__":
    app = ShosekiGUI()
    app.mainloop()
