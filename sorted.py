import matplotlib.pyplot as plt
import numpy as np
import io

def draw_complexity_chart(data, top_n=None):
    departments = data['departments']
    datasets = data['datasets']

    num_departments = len(departments)
    if num_departments == 0:
        return None

    colors = {
        "employee": "#808080", 
        "low": "#28a745",    
        "medium": "#ffc107",   
        "high": "#dc3545",    
        "naumen": "#007bff"   
    }
    
    labels = {
        "employee": "Мог решить самостоятельно",
        "low": "Низкая",
        "medium": "Средняя",
        "high": "Высокая",
        "naumen": "Наумен"
    }

    stack_order = ["employee", "low", "medium", "high", "naumen"]
    totals = []
    for i in range(num_departments):
        total_val = 0
        for status in stack_order:
            val_list = datasets.get(status, [])
            val = val_list[i] if i < len(val_list) else 0
            total_val += val
        totals.append((i, total_val))

    totals.sort(key=lambda x: x[1], reverse=True)
    if top_n and top_n < len(totals):
        totals = totals[:top_n]
        num_departments = top_n

    sorted_indices = [x[0] for x in totals]
    sorted_departments = [departments[i] for i in sorted_indices]
    fig_height = max(6, 2 + num_departments * 0.4)
    fig, ax = plt.subplots(figsize=(12, fig_height))

    left_offset = np.zeros(num_departments)
    y_pos = np.arange(num_departments)

    for status in stack_order:
        original_values = datasets.get(status, [])
        if not original_values:
             current_values = np.zeros(num_departments)
        else:
            current_values = []
            for idx in sorted_indices:
                if idx < len(original_values):
                    current_values.append(original_values[idx])
                else:
                    current_values.append(0)
            current_values = np.array(current_values)

        ax.barh(
            y_pos, 
            current_values, 
            left=left_offset, 
            label=labels[status], 
            color=colors[status],
            height=0.7,
            edgecolor='white',
            linewidth=0.5
        )
        left_offset += current_values

    ax.set_title('Сложность заявок по отделениям (Топ загруженных)', fontsize=14, pad=20)
    ax.set_xlabel('Количество заявок', fontsize=12)
    
    ax.set_yticks(y_pos)
    ax.set_yticklabels(sorted_departments, fontsize=10)
    ax.invert_yaxis()
    
    ax.grid(axis='x', linestyle='--', alpha=0.6)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.05),
              fancybox=True, shadow=True, ncol=len(stack_order))

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    
    return buf