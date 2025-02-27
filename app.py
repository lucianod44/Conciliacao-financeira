import pandas as pd
import cx_Oracle
from flask import Flask, render_template, request, redirect, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)

def connect_to_oracle():
    """Conecta ao banco de dados Oracle"""
    try:
        dsn = cx_Oracle.makedsn("luciano.corp.medgrupo.net", 1515, service_name="luciano")
        connection = cx_Oracle.connect(user="lcim", password="12345678", dsn=dsn)
        print("Conexão bem-sucedida ao banco de dados Oracle!")
        return connection
    except cx_Oracle.DatabaseError as e:
        print("Erro ao conectar ao banco de dados Oracle:", e)
        return None

@app.route('/consultar', methods=['GET', 'POST'])
def upload_file():
    table_html = None  
    empresas = []
    error_message = None  # Inicializa a mensagem de erro

    conn = connect_to_oracle()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT CD_MULTI_EMPRESA, DS_MULTI_EMPRESA 
        FROM MULTI_EMPRESAS 
        WHERE CD_MULTI_EMPRESA IN (1,2,3,4,9,11,12,14) 
        ORDER BY DS_MULTI_EMPRESA
    """)
    empresas = [{"CD_MULTI_EMPRESA": row[0], "DS_MULTI_EMPRESA": row[1]} for row in cursor.fetchall()]

    if request.method == 'POST':
        if 'file' not in request.files or 'empresa' not in request.form:
            error_message = "Erro: Selecione uma empresa e um arquivo!"
        else:
            file = request.files['file']
            empresa_id = request.form['empresa']

            if file.filename == '':
                error_message = "Nenhum arquivo selecionado."
            else:
                try:
                    df = pd.read_excel(file)

                    if "CD_MULTI_EMPRESA" in df.columns:
                        empresas_no_arquivo = df["CD_MULTI_EMPRESA"].unique()
                        if len(empresas_no_arquivo) > 1 or int(empresa_id) not in empresas_no_arquivo:
                            error_message = f"Erro: O arquivo contém dados de empresa(s) diferente(s) da selecionada ({empresa_id})!"
                        else:
                            cursor.execute("""
                                BEGIN
                                    EXECUTE IMMEDIATE 'DROP TABLE TEMP_CONCILIACAO';
                                EXCEPTION
                                    WHEN OTHERS THEN NULL;
                                END;
                            """)

                            cursor.execute("""
                                CREATE TABLE TEMP_CONCILIACAO (
                                    NR_DOCUMENTO_IDENTIFICACAO VARCHAR2(20),
                                    DT_MOVIMENTACAO DATE,
                                    CD_MULTI_EMPRESA NUMBER(10)
                                )
                            """)
                            conn.commit()

                            for _, row in df.iterrows():
                                cursor.execute("""
                                    INSERT INTO TEMP_CONCILIACAO 
                                    (NR_DOCUMENTO_IDENTIFICACAO, DT_MOVIMENTACAO, CD_MULTI_EMPRESA)
                                    VALUES (:1, :2, :3)
                                """, (row["NR_DOCUMENTO_IDENTIFICACAO"], row["DT_MOVIMENTACAO"], row["CD_MULTI_EMPRESA"]))

                            conn.commit()
                            table_html = df.to_html(classes='table table-striped', index=False)
                    else:
                        error_message = "Erro: O arquivo não contém a coluna 'CD_MULTI_EMPRESA'."

                except Exception as e:
                    error_message = f"Erro ao processar o arquivo: {str(e)}"

    cursor.close()
    conn.close()

    return render_template('upload.html', table_html=table_html, empresas=empresas, error_message=error_message)

@app.route('/conciliar')
def conciliar():
    conn = connect_to_oracle()
    cursor = conn.cursor()

    # Consulta os detalhes das conciliações, incluindo SN_CONCILIADO
    query_conciliacoes = """
        SELECT T.NR_DOCUMENTO_IDENTIFICACAO, T.DT_MOVIMENTACAO, T.CD_MULTI_EMPRESA,
               M.CD_MOV_CONCOR AS CD_MOV_CONCOR_BANCO, M.VL_MOVIMENTACAO AS VL_MOV_BANCO,
               M.DT_MOVIMENTACAO AS DT_MOV_BANCO, M.DS_MOVIMENTACAO AS DS_MOV_BANCO,
               M.SN_CONCILIADO
        FROM MOV_CONCOR M 
        INNER JOIN TEMP_CONCILIACAO T 
        ON T.NR_DOCUMENTO_IDENTIFICACAO = M.NR_DOCUMENTO_IDENTIFICACAO
        AND T.DT_MOVIMENTACAO = M.DT_MOVIMENTACAO
        WHERE M.CD_MULTI_EMPRESA = T.CD_MULTI_EMPRESA
        ORDER BY T.NR_DOCUMENTO_IDENTIFICACAO, T.DT_MOVIMENTACAO
    """
    
    cursor.execute(query_conciliacoes)
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    conciliacao_resultados = [dict(zip(columns, row)) for row in rows]

    # Consulta o resumo da conciliação
    query_resumo = """
        SELECT 
            CASE 
                WHEN M.SN_CONCILIADO = 'N' THEN 'PENDENTE'
                WHEN M.SN_CONCILIADO = 'S' THEN 'CONCILIADO'
            END AS STATUS,
            COUNT(DISTINCT M.NR_DOCUMENTO_IDENTIFICACAO) AS QTD
        FROM MOV_CONCOR M
        INNER JOIN TEMP_CONCILIACAO T
            ON T.NR_DOCUMENTO_IDENTIFICACAO = M.NR_DOCUMENTO_IDENTIFICACAO
            AND T.DT_MOVIMENTACAO = M.DT_MOVIMENTACAO
        WHERE M.CD_MULTI_EMPRESA = T.CD_MULTI_EMPRESA
        GROUP BY M.SN_CONCILIADO
    """

    cursor.execute(query_resumo)
    resumo_conciliacao = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.close()
    conn.close()
    
    return render_template('conciliar.html', conciliacao_resultados=conciliacao_resultados, resumo_conciliacao=resumo_conciliacao)

@app.route('/executar_conciliacao', methods=['POST'])
def executar_conciliacao():
    selected_ids = request.form.get('conciliar', '')  # Pegando os IDs como string única

    print("IDs Recebidos:", selected_ids)  # Para depuração

    if not selected_ids:
        return "Erro: Nenhuma movimentação válida selecionada para conciliação!", 400

    # 🔹 Separar os IDs corretamente e remover duplicatas
    selected_ids_list = list(set([id.strip() for id in selected_ids.split(",") if id.strip().isdigit()]))

    if not selected_ids_list:
        return "Erro: Nenhuma movimentação válida após filtragem!", 400

    conn = connect_to_oracle()
    cursor = conn.cursor()

    selected_ids_str = ", ".join([f"'{id}'" for id in selected_ids_list])

    plsql_block = f"""
DECLARE 
    CURSOR C IS 
        SELECT 
            T.NR_DOCUMENTO_IDENTIFICACAO,
            T.CD_MULTI_EMPRESA AS CD_MULTI_EMPRESA_TEMP,  
            T.DT_MOVIMENTACAO,
            M.CD_MOV_CONCOR AS CD_MOV_CONCOR_BANCO,
            M.VL_MOVIMENTACAO AS VL_MOV_BANCO,
            M.DT_MOVIMENTACAO AS DT_MOV_BANCO,
            M.DS_MOVIMENTACAO AS DS_MOV_BANCO,
            M.SN_CONCILIADO,
            M.CD_MULTI_EMPRESA AS CD_MULTI_EMPRESA_MOV  
        FROM MOV_CONCOR M
        INNER JOIN TEMP_CONCILIACAO T
        ON T.NR_DOCUMENTO_IDENTIFICACAO = M.NR_DOCUMENTO_IDENTIFICACAO
        AND T.DT_MOVIMENTACAO = M.DT_MOVIMENTACAO
        WHERE M.CD_MULTI_EMPRESA = T.CD_MULTI_EMPRESA
        AND M.NR_DOCUMENTO_IDENTIFICACAO IN ({selected_ids_str})
        AND M.SN_CONCILIADO = 'N';

BEGIN
    FOR C1 IN C LOOP
        PKG_MV2000.ATRIBUI_EMPRESA(C1.CD_MULTI_EMPRESA_MOV); 
        
        UPDATE MOV_CONCOR 
        SET SN_CONCILIADO = 'S'
        WHERE CD_MULTI_EMPRESA = C1.CD_MULTI_EMPRESA_MOV 
        AND NR_DOCUMENTO_IDENTIFICACAO = C1.NR_DOCUMENTO_IDENTIFICACAO
        AND DT_MOVIMENTACAO = C1.DT_MOVIMENTACAO
        AND SN_CONCILIADO = 'N';
    END LOOP;
END;
"""

    try:
        cursor.execute(plsql_block)
        conn.commit()
    except cx_Oracle.DatabaseError as e:
        return f"Erro ao executar conciliação: {str(e)}", 400
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('conciliar'))


@app.route('/excluir_tabela', methods=['POST'])
def excluir_tabela():
    """Exclui a tabela temporária e redireciona para a página de upload"""
    conn = connect_to_oracle()
    cursor = conn.cursor()

    try:
        cursor.execute("""TRUNCATE TABLE TEMP_CONCILIACAO""")
        conn.commit()
    except Exception as e:
        print(f"Erro ao excluir a tabela: {e}")

    cursor.close()
    conn.close()

    return redirect(url_for('upload_file'))


if __name__ == '__main__':
    app.run(debug=True)