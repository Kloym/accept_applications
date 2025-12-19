import sqlite3

DB_NAME = "applications.db"
OLD_NAME = "Компьютерной обработки документов по расчетам обязательного медицинского страхования"
NEW_NAME = "Компьютерной обработки документов по расчетам ОМС"

def fix_department_names():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM applications WHERE department = ?", (OLD_NAME,))
        count = cursor.fetchone()[0]

        if count == 0:
            print(f"❌ Записей с названием '{OLD_NAME}' не найдено.")
            return

        print(f"Найдено {count} заявок со старым названием.")
        cursor.execute("UPDATE applications SET department = ? WHERE department = ?", (NEW_NAME, OLD_NAME))
        conn.commit()

        print(f"✅ Успешно! Все {count} заявки переименованы в '{NEW_NAME}'.")
        print("Теперь статистика объединится.")

    except sqlite3.Error as e:
        print(f"Ошибка базы данных: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    fix_department_names()